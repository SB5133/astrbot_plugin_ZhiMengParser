from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from .config import PluginConfig
    from .data import ParseResult


@dataclass(slots=True)
class _CacheEntry:
    """单个缓存项的内存索引"""

    key: str
    path: Path
    created_at: float
    accessed_at: float = field(default_factory=time.time)


class RenderCacheManager:
    """
    渲染结果缓存管理器

    特性：
    - 智能缓存键：基于 ParseResult 内容、样式配置、源媒体文件 mtime 生成 SHA256 哈希，
      内容变化或样式变化会自动失效。
    - TTL 过期：超过 TTL 未写入的缓存项视为失效。
    - 数量限制：超过最大数量时按 LRU（最近最少使用）淘汰，并删除对应文件。
    - 磁盘缓存：缓存文件落盘，插件重启后仍在（只要文件未过期/未被淘汰）。
    """

    def __init__(
        self,
        cache_dir: Path,
        ttl: int,
        max_count: int,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.cache_dir = cache_dir / "render"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = max(0, ttl)
        self.max_count = max(0, max_count)

        # 内存索引：OrderedDict 方便按访问顺序做 LRU
        self._index: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._meta_file = self.cache_dir / "index.json"

        if enabled:
            self._load_index()

    def _entry_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.png"

    def _load_index(self) -> None:
        """从元数据文件加载缓存索引，并清理已不存在的文件"""
        if not self._meta_file.exists():
            return
        try:
            raw = json.loads(self._meta_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[RenderCache] 加载索引失败: {e}")
            return

        now = time.time()
        for key, item in raw.items():
            try:
                path = self._entry_path(key)
                if not path.exists():
                    continue
                created = float(item.get("created_at", 0))
                if self.ttl > 0 and now - created > self.ttl:
                    path.unlink(missing_ok=True)
                    continue
                entry = _CacheEntry(
                    key=key,
                    path=path,
                    created_at=created,
                    accessed_at=float(item.get("accessed_at", created)),
                )
                self._index[key] = entry
            except Exception:
                continue

        self._persist_index()
        logger.debug(f"[RenderCache] 已加载 {len(self._index)} 个缓存项")

    def _persist_index(self) -> None:
        """将内存索引持久化到元数据文件"""
        try:
            data = {
                key: {
                    "created_at": entry.created_at,
                    "accessed_at": entry.accessed_at,
                }
                for key, entry in self._index.items()
            }
            self._meta_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[RenderCache] 保存索引失败: {e}")

    def _evict_if_needed(self) -> None:
        """超过数量限制时，淘汰最久未访问的项"""
        while self.max_count > 0 and len(self._index) >= self.max_count:
            key, entry = self._index.popitem(last=False)
            entry.path.unlink(missing_ok=True)
            logger.debug(f"[RenderCache] LRU 淘汰: {key}")

    def _cleanup_expired(self) -> None:
        """清理已过期缓存项"""
        if self.ttl <= 0:
            return
        now = time.time()
        expired = [
            key
            for key, entry in self._index.items()
            if now - entry.created_at > self.ttl
        ]
        for key in expired:
            entry = self._index.pop(key, None)
            if entry:
                entry.path.unlink(missing_ok=True)
                logger.debug(f"[RenderCache] TTL 清理: {key}")
        if expired:
            self._persist_index()

    async def compute_key(
        self,
        result: "ParseResult",
        cfg: "PluginConfig",
    ) -> str:
        """
        计算智能缓存键

        输入包括：
        - 资源指纹（平台、URL、时间、作者、内容结构、转发）
        - 标题、正文、额外信息
        - 卡片样式配置
        - 参与渲染的源媒体文件最后修改时间
        """
        h = hashlib.sha256()

        # 1. 资源指纹 + 文本内容
        h.update(result.get_resource_id().encode("utf-8"))
        h.update(b"\x00")
        h.update((result.title or "").encode("utf-8"))
        h.update(b"\x00")
        h.update((result.text or "").encode("utf-8"))
        h.update(b"\x00")
        h.update(json.dumps(result.extra, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        h.update(b"\x00")

        # 2. 样式配置
        style = {
            "card_bg_image": cfg.card_bg_image,
            "card_bg_blur": cfg.card_bg_blur,
            "card_glass_opacity": cfg.card_glass_opacity,
        }
        h.update(json.dumps(style, sort_keys=True).encode("utf-8"))
        h.update(b"\x00")

        # 3. 参与渲染的媒体文件 mtime
        files: list[Path] = []

        cover = await result.cover_path
        if cover:
            files.append(cover)

        if result.author:
            avatar = await result.author.get_avatar_path()
            if avatar:
                files.append(avatar)

        for cont in result.contents:
            try:
                if cont.__class__.__name__ in ("ImageContent", "GraphicsContent"):
                    files.append(await cont.get_path())
                elif cont.__class__.__name__ == "VideoContent":
                    cover_path = await cont.get_cover_path()
                    if cover_path:
                        files.append(cover_path)
            except Exception:
                continue

        # 加入 repost 中的媒体
        if result.repost:
            h.update((await self.compute_key(result.repost, cfg)).encode("utf-8"))

        for path in sorted(set(files)):
            try:
                mtime = path.stat().st_mtime_ns
                h.update(f"{path.as_posix()}:{mtime}\n".encode("utf-8"))
            except Exception:
                h.update(f"{path.as_posix()}\n".encode("utf-8"))

        return h.hexdigest()

    def get(self, key: str) -> Path | None:
        """查询缓存，命中时更新访问时间并返回路径"""
        if not self.enabled or self.max_count <= 0:
            return None
        self._cleanup_expired()

        entry = self._index.get(key)
        if entry is None:
            return None
        if self.ttl > 0 and time.time() - entry.created_at > self.ttl:
            self._index.pop(key, None)
            entry.path.unlink(missing_ok=True)
            self._persist_index()
            return None
        if not entry.path.exists():
            self._index.pop(key, None)
            self._persist_index()
            return None

        entry.accessed_at = time.time()
        self._index.move_to_end(key)
        self._persist_index()
        logger.debug(f"[RenderCache] 命中: {key}")
        return entry.path

    def set(self, key: str, path: Path) -> Path:
        """写入缓存，必要时淘汰旧项，返回最终缓存路径"""
        if not self.enabled or self.max_count <= 0:
            return path

        self._cleanup_expired()
        self._evict_if_needed()

        target = self._entry_path(key)
        try:
            if path.resolve() != target.resolve():
                # 使用硬链接减少拷贝；失败则复制
                try:
                    target.unlink(missing_ok=True)
                    target.hardlink_to(path)
                except OSError:
                    import shutil

                    shutil.copy2(path, target)
        except Exception as e:
            logger.warning(f"[RenderCache] 写入缓存文件失败: {e}")
            return path

        now = time.time()
        entry = _CacheEntry(key=key, path=target, created_at=now, accessed_at=now)
        self._index[key] = entry
        self._index.move_to_end(key)
        self._persist_index()
        logger.debug(f"[RenderCache] 写入: {key}")
        return target

    async def clear(self) -> int:
        """清空所有缓存，返回删除文件数"""
        count = 0
        for entry in self._index.values():
            if entry.path.exists():
                entry.path.unlink(missing_ok=True)
                count += 1
        self._index.clear()
        self._persist_index()
        return count

    def stats(self) -> dict[str, int | float]:
        """返回缓存统计"""
        return {
            "count": len(self._index),
            "max_count": self.max_count,
            "ttl": self.ttl,
            "enabled": int(self.enabled),
        }


class VideoCacheManager:
    """
    视频下载结果缓存管理器

    缓存合并后的完整视频文件，命中时直接返回本地路径。
    缓存键：hash(原始URL + 文件大小 + 分辨率 + 压缩品质模式 + CRF值)
    """

    def __init__(
        self,
        cache_dir: Path,
        ttl: int,
        max_count: int,
        enabled: bool = True,
    ):
        self.enabled = enabled
        self.cache_dir = cache_dir / "videos"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = max(0, ttl)
        self.max_count = max(0, max_count)

        self._index: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._meta_file = self.cache_dir / "index.json"

        if enabled:
            self._load_index()

    def _entry_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.mp4"

    def _load_index(self) -> None:
        if not self._meta_file.exists():
            return
        try:
            raw = json.loads(self._meta_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[VideoCache] 加载索引失败: {e}")
            return

        now = time.time()
        for key, item in raw.items():
            try:
                path = self._entry_path(key)
                if not path.exists():
                    continue
                created = float(item.get("created_at", 0))
                if self.ttl > 0 and now - created > self.ttl:
                    path.unlink(missing_ok=True)
                    continue
                entry = _CacheEntry(
                    key=key,
                    path=path,
                    created_at=created,
                    accessed_at=float(item.get("accessed_at", created)),
                )
                self._index[key] = entry
            except Exception:
                continue

        self._persist_index()
        logger.debug(f"[VideoCache] 已加载 {len(self._index)} 个缓存项")

    def _persist_index(self) -> None:
        try:
            data = {
                key: {
                    "created_at": entry.created_at,
                    "accessed_at": entry.accessed_at,
                }
                for key, entry in self._index.items()
            }
            self._meta_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[VideoCache] 保存索引失败: {e}")

    def _evict_if_needed(self) -> None:
        while self.max_count > 0 and len(self._index) >= self.max_count:
            key, entry = self._index.popitem(last=False)
            entry.path.unlink(missing_ok=True)
            logger.debug(f"[VideoCache] LRU 淘汰: {key}")

    def _cleanup_expired(self) -> None:
        if self.ttl <= 0:
            return
        now = time.time()
        expired = [
            key
            for key, entry in self._index.items()
            if now - entry.created_at > self.ttl
        ]
        for key in expired:
            entry = self._index.pop(key, None)
            if entry:
                entry.path.unlink(missing_ok=True)
                logger.debug(f"[VideoCache] TTL 清理: {key}")
        if expired:
            self._persist_index()

    @staticmethod
    def compute_key(
        url: str,
        size: int,
        resolution: str | None,
        quality_mode: str | None,
        quality_value: str | int | None,
    ) -> str:
        """计算视频缓存键"""
        h = hashlib.sha256()
        h.update(url.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(size).encode("utf-8"))
        h.update(b"\x00")
        h.update((resolution or "unknown").encode("utf-8"))
        h.update(b"\x00")
        h.update((quality_mode or "unknown").encode("utf-8"))
        h.update(b"\x00")
        h.update(str(quality_value or "unknown").encode("utf-8"))
        return h.hexdigest()

    def get(self, key: str) -> Path | None:
        """查询缓存"""
        if not self.enabled or self.max_count <= 0:
            return None
        self._cleanup_expired()

        entry = self._index.get(key)
        if entry is None:
            return None
        if self.ttl > 0 and time.time() - entry.created_at > self.ttl:
            self._index.pop(key, None)
            entry.path.unlink(missing_ok=True)
            self._persist_index()
            return None
        if not entry.path.exists():
            self._index.pop(key, None)
            self._persist_index()
            return None

        entry.accessed_at = time.time()
        self._index.move_to_end(key)
        self._persist_index()
        logger.debug(f"[VideoCache] 命中: {key}")
        return entry.path

    def set(self, key: str, path: Path) -> Path:
        """写入缓存"""
        if not self.enabled or self.max_count <= 0:
            return path

        self._cleanup_expired()
        self._evict_if_needed()

        target = self._entry_path(key)
        try:
            if path.resolve() != target.resolve():
                try:
                    target.unlink(missing_ok=True)
                    target.hardlink_to(path)
                except OSError:
                    import shutil

                    shutil.copy2(path, target)
        except Exception as e:
            logger.warning(f"[VideoCache] 写入缓存文件失败: {e}")
            return path

        now = time.time()
        entry = _CacheEntry(key=key, path=target, created_at=now, accessed_at=now)
        self._index[key] = entry
        self._index.move_to_end(key)
        self._persist_index()
        logger.debug(f"[VideoCache] 写入: {key}")
        return target

    def clear(self) -> int:
        """清空缓存"""
        count = 0
        for entry in self._index.values():
            if entry.path.exists():
                entry.path.unlink(missing_ok=True)
                count += 1
        self._index.clear()
        self._persist_index()
        return count

    def stats(self) -> dict[str, int | float]:
        return {
            "count": len(self._index),
            "max_count": self.max_count,
            "ttl": self.ttl,
            "enabled": int(self.enabled),
        }
