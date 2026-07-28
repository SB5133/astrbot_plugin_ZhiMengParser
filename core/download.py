import asyncio
import random
import time
from asyncio import Task, TimeoutError, create_task, gather, sleep, to_thread
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import Any, AsyncGenerator, List, Optional, ParamSpec, TypeVar
from urllib.parse import urlparse

import aiofiles
import yt_dlp
from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector
from msgspec import Struct, convert
from tqdm.asyncio import tqdm

from astrbot.api import logger

from .cache import VideoCacheManager
from .compress import ENCODER_CONFIG, ENCODER_NAME_MAP, QUALITY_PRESETS, RESOLUTION_MAP
from .config import PluginConfig
from .constants import COMMON_HEADER
from .exception import (
    DownloadException,
    DurationLimitException,
    ParseException,
    SizeLimitException,
    ZeroSizeException,
)
from .memory_monitor import MemoryMonitor
from .utils import (
    LimitedSizeDict,
    generate_file_name,
    merge_av,
    merge_av_streaming,
    memory_info,
    safe_unlink,
    sanitize_url,
)

P = ParamSpec("P")
T = TypeVar("T")


# 智能阈值常量
SMALL_FILE_THRESHOLD = 10 * 1024 * 1024  # 10MB：跳过所有优化
NO_COMPRESS_THRESHOLD = 15 * 1024 * 1024  # 15MB：不压缩
NO_CDN_THRESHOLD = 20 * 1024 * 1024  # 20MB：跳过CDN测速
CDN_TEST_SIZE = 256 * 1024  # 256KB
CDN_CACHE_TTL = 300  # 5分钟
CDN_RETEST_RATIO = 0.5  # 实际速度低于测速50%时重新测速
CDN_RETEST_AGE = 180  # 3分钟

# 下载错误分类
ERROR_NETWORK_JITTER = {"timeout", "connection_refused", "incomplete"}
ERROR_RATE_LIMIT = {"rate_limit"}


def auto_task(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Task[T]]:
    """装饰器：自动将异步函数调用转换为 Task, 完整保留类型提示"""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> Task[T]:
        coro = func(*args, **kwargs)
        name = " | ".join(str(arg) for arg in args if isinstance(arg, str))
        return create_task(coro, name=func.__name__ + " | " + name)

    return wrapper


class VideoInfo(Struct):
    title: str
    """标题"""
    channel: str
    """频道名称"""
    uploader: str
    """上传者 id"""
    duration: int
    """时长"""
    timestamp: int
    """发布时间戳"""
    thumbnail: str
    """封面图片"""
    description: str
    """简介"""
    channel_id: str
    """频道 id"""

    @property
    def author_name(self) -> str:
        return f"{self.channel}@{self.uploader}"


class AdaptiveSemaphoreManager:
    """平台级自适应下载并发管理器

    - 每个平台拥有独立的 asyncio.Semaphore
    - 支持在解析器配置中单独设置并发数
    - 连续下载失败达到阈值时自动降低并发
    - 下载成功后按间隔逐步恢复并发
    """

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._current: dict[str, int] = {}
        self._fail_streak: dict[str, int] = {}
        self._last_change: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _get_default_concurrency(self, platform: str) -> int:
        """获取平台默认并发数（优先使用解析器单独配置）"""
        try:
            parser_cfg = getattr(self.cfg.parser, platform)
            custom = parser_cfg.download_concurrency
            if custom is not None and custom > 0:
                return custom
        except AttributeError:
            pass
        return self.cfg.perf_download_default_concurrency

    def _ensure_sem(self, platform: str) -> asyncio.Semaphore:
        if platform not in self._semaphores:
            init = self._get_default_concurrency(platform)
            self._semaphores[platform] = asyncio.Semaphore(init)
            self._current[platform] = init
        return self._semaphores[platform]

    @asynccontextmanager
    async def acquire(self, platform: str | None) -> AsyncGenerator[None, None]:
        """获取指定平台的下载许可；未启用自适应或平台未知时直接通过"""
        if not platform or not self.cfg.perf_adaptive_download:
            yield
            return
        sem = self._ensure_sem(platform)
        async with sem:
            yield

    async def report_success(self, platform: str | None) -> None:
        if not platform or not self.cfg.perf_adaptive_download:
            return
        async with self._lock:
            self._fail_streak[platform] = 0
            now = time.time()
            interval = self.cfg.perf_download_recover_interval
            last = self._last_change.get(platform, 0)
            if interval > 0 and now - last < interval:
                return
            cur = self._current.get(platform, self._get_default_concurrency(platform))
            target = min(
                cur + self.cfg.perf_download_recover_step,
                self._get_default_concurrency(platform),
            )
            if target != cur:
                self._resize(platform, target)
                self._last_change[platform] = now
                logger.info(
                    f"[AdaptiveDownload] {platform} 下载成功，并发从 {cur} 恢复至 {target}"
                )

    async def report_failure(self, platform: str | None) -> None:
        if not platform or not self.cfg.perf_adaptive_download:
            return
        async with self._lock:
            self._fail_streak[platform] = self._fail_streak.get(platform, 0) + 1
            if self._fail_streak[platform] < self.cfg.perf_download_fail_threshold:
                return
            self._fail_streak[platform] = 0
            cur = self._current.get(platform, self._get_default_concurrency(platform))
            target = max(
                cur - self.cfg.perf_download_degrade_step,
                self.cfg.perf_download_min_concurrency,
            )
            if target != cur:
                self._resize(platform, target)
                self._last_change[platform] = time.time()
                logger.warning(
                    f"[AdaptiveDownload] {platform} 连续失败达到阈值，并发从 {cur} 降至 {target}"
                )

    def _resize(self, platform: str, new_size: int) -> None:
        """调整指定平台的信号量容量"""
        self._semaphores[platform] = asyncio.Semaphore(new_size)
        self._current[platform] = new_size


class Downloader:
    """下载器，支持 youtube-dlp 和流式下载，集成 CDN 优选、分块下载、流式压缩、视频缓存"""

    def __init__(self, config: PluginConfig):
        self.cfg = config
        self.max_size = self.cfg.source_max_size
        self.default_headers: dict[str, str] = COMMON_HEADER.copy()
        # 视频信息缓存
        self.info_cache: LimitedSizeDict[str, VideoInfo] = LimitedSizeDict()

        # 全局连接池：复用 TCP 连接，限制总连接数和单主机连接数
        self._connector = TCPConnector(
            limit=20,
            limit_per_host=10,
            ttl_dns_cache=300,
            use_dns_cache=True,
        )
        self.client = ClientSession(
            connector=self._connector,
            timeout=ClientTimeout(total=self.cfg.download_timeout),
        )
        # 自适应并发管理器
        self.adaptive = AdaptiveSemaphoreManager(config)

        # 全局下载信号量（控制同时下载数）
        self._semaphore = asyncio.Semaphore(self.cfg.download_concurrency_limit)

        # 可选组件（由 main.py 注入）
        self.video_cache: VideoCacheManager | None = None
        self.memory_monitor: MemoryMonitor | None = None

        # CDN 测速结果缓存: url -> {"speed": bytes/s, "ts": float, "node": str}
        self._cdn_speed_cache: LimitedSizeDict[str, dict[str, Any]] = LimitedSizeDict(
            max_size=100
        )
        # 分块下载风控: platform -> [失败时间戳列表]
        self._range_failures: dict[str, list[float]] = {}
        # 分块下载禁用至: platform -> timestamp
        self._range_disabled_until: dict[str, float] = {}

        # 动态并发统计
        self._dynamic_concurrency_enabled = bool(self.cfg.download_dynamic_concurrency)
        self._current_concurrency = int(self.cfg.download_concurrency_limit)
        self._download_results: list[bool] = []
        self._dyn_lock = asyncio.Lock()

    def set_video_cache(self, cache: VideoCacheManager | None) -> None:
        self.video_cache = cache

    def set_memory_monitor(self, monitor: MemoryMonitor | None) -> None:
        self.memory_monitor = monitor

    async def close(self):
        """关闭网络客户端"""
        await self.client.close()

    # ---------- URL 与节点工具 ----------

    @staticmethod
    def _extract_host(url: str) -> str:
        """从 URL 中提取 host（含端口）作为 CDN 节点标识"""
        try:
            parsed = urlparse(url)
            if parsed.hostname:
                port = parsed.port
                if port and (
                    (parsed.scheme == "http" and port != 80)
                    or (parsed.scheme == "https" and port != 443)
                ):
                    return f"{parsed.hostname}:{port}"
                return parsed.hostname
        except Exception:
            pass
        return url

    @staticmethod
    def _classify_download_error(exc: Exception) -> str:
        """将下载异常分类为连接被拒/超时/不完整/限流/其他"""
        if isinstance(exc, TimeoutError):
            return "timeout"
        msg = str(exc).lower()
        if "429" in msg or "rate limit" in msg or "too many requests" in msg:
            return "rate_limit"
        if "payload incomplete" in msg or "incomplete" in msg:
            return "incomplete"
        if "connection refused" in msg or "cannot connect to host" in msg:
            return "connection_refused"
        if "timeout" in msg or "timed out" in msg:
            return "timeout"
        return "other"

    def _get_headers(self, platform: str | None) -> dict[str, str]:
        """根据平台返回对应的请求头（Referer / Origin）"""
        headers: dict[str, str] = {}
        if platform == "bilibili":
            headers["Referer"] = "https://www.bilibili.com/"
            headers["Origin"] = "https://www.bilibili.com"
        elif platform == "douyin":
            headers["Referer"] = "https://www.douyin.com/"
            headers["Origin"] = "https://www.douyin.com"
        elif platform == "kuaishou":
            headers["Referer"] = "https://www.kuaishou.com/"
            headers["Origin"] = "https://www.kuaishou.com"
        elif platform == "xhs":
            headers["Referer"] = "https://www.xiaohongshu.com/"
            headers["Origin"] = "https://www.xiaohongshu.com"
        elif platform == "weibo":
            headers["Referer"] = "https://weibo.com/"
            headers["Origin"] = "https://weibo.com"
        elif platform == "twitter":
            headers["Referer"] = "https://twitter.com/"
            headers["Origin"] = "https://twitter.com"
        elif platform == "youtube":
            headers["Referer"] = "https://www.youtube.com/"
            headers["Origin"] = "https://www.youtube.com"
        elif platform == "instagram":
            headers["Referer"] = "https://www.instagram.com/"
            headers["Origin"] = "https://www.instagram.com"
        return headers

    # ---------- 智能阈值与分块数 ----------

    def _smart_threshold(self, size: int | None) -> dict[str, bool]:
        """根据文件大小决定启用哪些优化"""
        if size is None:
            return {"cdn": False, "range": True, "compress": True}
        if size < SMALL_FILE_THRESHOLD:
            return {"cdn": False, "range": False, "compress": False}
        if size < NO_COMPRESS_THRESHOLD:
            return {"cdn": True, "range": True, "compress": False}
        if size < NO_CDN_THRESHOLD:
            return {"cdn": False, "range": True, "compress": True}
        return {"cdn": True, "range": True, "compress": True}

    def _auto_parts(self, size: int) -> int:
        """根据文件大小自动分配分块数"""
        if size < 10 * 1024 * 1024:
            return 1
        if size < 50 * 1024 * 1024:
            return 2
        if size < 100 * 1024 * 1024:
            return 3
        if size < 500 * 1024 * 1024:
            return 4
        if size < 1024 * 1024 * 1024:
            return 6
        return 8

    def _effective_max_parts(self, platform: str | None, file_size: int) -> int:
        """计算实际分块数：取自动分配值、用户上限（含平台覆盖、内存监控降级）的较小值"""
        user_max = self.cfg.range_download_max_parts
        if platform:
            platform_overrides = self.cfg.range_download_max_parts_platforms or {}
            if platform in platform_overrides:
                user_max = max(1, int(platform_overrides[platform]))

        # 内存严重不足时完全关闭分块
        if self.memory_monitor is not None and self.cfg.range_memory_auto_disable:
            if self.memory_monitor.memory_pressure:
                return 1

        if self.memory_monitor is not None:
            user_max = self.memory_monitor.get_current_max_parts(user_max)

        auto = self._auto_parts(file_size)
        return max(1, min(auto, user_max))

    def _range_disabled(self, platform: str | None) -> bool:
        """检查当前平台是否因风控被禁用分块下载"""
        if not platform:
            return False
        until = self._range_disabled_until.get(platform, 0)
        if time.time() < until:
            return True
        return False

    def _record_range_failure(self, platform: str | None) -> None:
        """记录分块下载失败，连续3次后禁用10分钟"""
        if not platform:
            return
        now = time.time()
        self._range_failures.setdefault(platform, []).append(now)
        # 只保留10分钟内的失败
        self._range_failures[platform] = [
            t for t in self._range_failures[platform] if now - t < 600
        ]
        if len(self._range_failures[platform]) >= 3:
            self._range_disabled_until[platform] = now + 600
            logger.warning(
                f"[RangeDownload] {platform} 分块下载连续失败3次，禁用10分钟"
            )

    def _clear_range_failure(self, platform: str | None) -> None:
        if platform and platform in self._range_failures:
            del self._range_failures[platform]

    # ---------- 动态并发调整 ----------

    async def _record_download_result(self, success: bool) -> None:
        """记录下载结果，根据成功率动态调整全局并发数"""
        if not self._dynamic_concurrency_enabled:
            return
        async with self._dyn_lock:
            self._download_results.append(success)
            # 保留最近 50 次结果
            if len(self._download_results) > 50:
                self._download_results = self._download_results[-50:]
            total = len(self._download_results)
            if total < 10:
                return
            success_rate = sum(self._download_results) / total
            old = self._current_concurrency
            if success_rate > 0.95 and old < 10:
                self._current_concurrency = min(10, old + 1)
            elif success_rate < 0.90 and old > 2:
                self._current_concurrency = max(2, old - 1)
            if self._current_concurrency != old:
                self._semaphore = asyncio.Semaphore(self._current_concurrency)
                logger.info(
                    f"[Download] 动态并发调整 | success_rate={success_rate:.1%} | "
                    f"concurrency={old} -> {self._current_concurrency}"
                )

    # ---------- 文件大小探测 ----------

    async def _get_file_size(
        self,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
    ) -> int | None:
        """通过 HEAD 请求获取文件大小"""
        try:
            async with self.client.head(
                url, headers=headers, allow_redirects=True, proxy=proxy
            ) as response:
                if response.status >= 400:
                    return None
                length = response.headers.get("Content-Length")
                if length:
                    return int(length)
        except Exception as e:
            self.cfg.verbose(
                f"[Download] HEAD 获取大小失败 | url={sanitize_url(url)} | reason={e}"
            )
        return None

    # ---------- CDN 优选 ----------

    async def _test_node_speed(
        self,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
    ) -> float:
        """对节点进行快速测速，返回速度 MB/s（0 表示失败或不可接受）"""
        start = time.time()
        downloaded = 0
        try:
            async with self.client.get(
                url,
                headers={**headers, "Range": f"bytes=0-{CDN_TEST_SIZE - 1}"},
                allow_redirects=True,
                proxy=proxy,
                timeout=ClientTimeout(total=10),
            ) as response:
                if response.status >= 400:
                    return 0.0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    downloaded += len(chunk)
                    if downloaded >= CDN_TEST_SIZE:
                        break
            elapsed = time.time() - start
            speed_bps = downloaded / elapsed if elapsed > 0 else downloaded
            speed_mbps = speed_bps / 1024 / 1024
            host = self._extract_host(url)
            self.cfg.verbose(
                f"[CDN] 测速 | node={host} | speed={speed_mbps:.2f}MB/s"
            )
            return speed_mbps
        except Exception as e:
            self.cfg.verbose(
                f"[CDN] 测速失败 | url={sanitize_url(url)} | reason={e}"
            )
            return 0.0

    async def _cdn_speed_test_one(
        self,
        url: str,
        headers: dict[str, str],
        proxy: str | None,
    ) -> tuple[str, float] | None:
        """对单个 URL 测速，下载 256KB，返回 (url, speed_bytes_per_sec)"""
        speed_mbps = await self._test_node_speed(url, headers, proxy)
        if speed_mbps <= 0:
            return None
        speed_bps = speed_mbps * 1024 * 1024
        return url, speed_bps

    async def _select_best_cdn(
        self,
        urls: list[str],
        headers: dict[str, str],
        proxy: str | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """从多个 URL 中选择速度最快的，返回 (best_url, cached_chunk_data)"""
        if len(urls) <= 1:
            return urls[0], None

        now = time.time()
        # 优先使用缓存结果
        cached_best: str | None = None
        cached_speed = 0.0
        for url in urls:
            entry = self._cdn_speed_cache.get(url)
            if entry and now - entry.get("ts", 0) < CDN_CACHE_TTL:
                if entry.get("speed", 0) > cached_speed:
                    cached_speed = entry["speed"]
                    cached_best = url

        if cached_best:
            self.cfg.verbose(
                f"[CDN] 使用缓存测速结果 | node={self._extract_host(cached_best)} | "
                f"speed={cached_speed / 1024:.1f}KB/s"
            )
            return cached_best, None

        results = await gather(
            *[self._cdn_speed_test_one(u, headers, proxy) for u in urls],
            return_exceptions=True,
        )
        best_url: str | None = None
        best_speed = -1.0
        for res in results:
            if isinstance(res, Exception) or res is None:
                continue
            u, speed = res
            self._cdn_speed_cache[u] = {"speed": speed, "ts": time.time(), "node": self._extract_host(u)}
            if speed > best_speed:
                best_speed = speed
                best_url = u

        if best_url:
            self.cfg.verbose(
                f"[CDN] 选择最优节点 | node={self._extract_host(best_url)} | "
                f"speed={best_speed / 1024:.1f}KB/s"
            )
            return best_url, None

        logger.warning("[CDN] 所有节点测速失败，使用第一个 URL")
        return urls[0], None

    # ---------- 分块下载 ----------

    async def _download_part(
        self,
        url: str,
        file_path: Path,
        start: int,
        end: int,
        headers: dict[str, str],
        proxy: str | None,
        platform: str | None,
        part_idx: int,
    ) -> bool:
        """下载一个分块到文件指定位置。

        支持断点续传：
        - 临时文件 ``<file>.part_<part_idx>.tmp`` 记录该分块当前已下载字节数。
        - 启动时若临时文件存在，则从 ``start + downloaded`` 处继续下载。
        - 当服务器返回 200（不支持 Range）时，自动回退到完整下载（直接返回 False 让上层走单连接）。
        """
        part_headers = dict(headers)
        part_size = end - start + 1
        tmp_path = file_path.with_name(f"{file_path.name}.part_{part_idx}.tmp")
        resume_enabled = bool(getattr(self.cfg, "range_download_resume_enabled", True))

        # 计算已下载进度（基于临时文件）
        downloaded = 0
        if resume_enabled and tmp_path.exists():
            try:
                downloaded = tmp_path.stat().st_size
                if downloaded >= part_size:
                    # 该分块已完成，直接合并到最终文件
                    logger.info(
                        f"[RangeDownload] 分块 {file_path.name} part_{part_idx} 已完成，跳过"
                    )
                    async with aiofiles.open(tmp_path, "rb") as src, aiofiles.open(
                        file_path, "r+b"
                    ) as dst:
                        await dst.seek(start)
                        await src.seek(0)
                        while True:
                            buf = await src.read(1024 * 1024)
                            if not buf:
                                break
                            await dst.write(buf)
                    await safe_unlink(tmp_path)
                    return True
                if downloaded > 0:
                    resume_start = start + downloaded
                    logger.warning(
                        f"[RangeDownload] 检测到未完成的分块: {file_path.name} "
                        f"part_{part_idx}（已下载 {downloaded / 1024 / 1024:.2f}MB/"
                        f"{part_size / 1024 / 1024:.2f}MB），继续下载"
                    )
                    part_headers["Range"] = f"bytes={resume_start}-{end}"
                else:
                    part_headers["Range"] = f"bytes={start}-{end}"
            except Exception as e:
                self.cfg.verbose(
                    f"[RangeDownload] 检查临时分块文件失败: {e}"
                )
                downloaded = 0
                part_headers["Range"] = f"bytes={start}-{end}"
        else:
            part_headers["Range"] = f"bytes={start}-{end}"

        for attempt in range(3):
            try:
                async with self.client.get(
                    url,
                    headers=part_headers,
                    allow_redirects=True,
                    proxy=proxy,
                ) as response:
                    # 服务器返回 200 说明不支持 Range 请求
                    if response.status == 200 and "Range" in part_headers:
                        logger.warning(
                            f"[RangeDownload] 服务器不支持 Range 请求，回退到完整下载"
                        )
                        await safe_unlink(tmp_path)
                        return False
                    if response.status in (403, 416):
                        logger.warning(
                            f"[RangeDownload] {platform or '?'} 分块 {part_idx} 返回 {response.status}，回退单连接"
                        )
                        await safe_unlink(tmp_path)
                        return False
                    if response.status >= 400:
                        raise ClientError(f"HTTP {response.status}")

                    write_offset = start + downloaded
                    async with aiofiles.open(tmp_path, "wb") as tmpf:
                        await tmpf.seek(downloaded)
                        received = 0
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            await tmpf.write(chunk)
                            received += len(chunk)
                            downloaded += len(chunk)
                    # 写入最终文件
                    async with aiofiles.open(file_path, "r+b") as f:
                        await f.seek(write_offset)
                        async with aiofiles.open(tmp_path, "rb") as tmpf:
                            await tmpf.seek(0)
                            await f.write(await tmpf.read())

                # 校验下载大小
                if downloaded < part_size:
                    # 文件未完整，继续重试（保留 tmp 状态）
                    if attempt < 2:
                        wait = 1 + attempt
                        self.cfg.verbose(
                            f"[RangeDownload] 分块 {part_idx} 不完整 ({downloaded}/{part_size})，{wait}s 后重试"
                        )
                        await sleep(wait)
                        continue
                    logger.warning(
                        f"[RangeDownload] 分块 {part_idx} 最终不完整 ({downloaded}/{part_size})，回退单连接"
                    )
                    await safe_unlink(tmp_path)
                    return False

                # 完成：清理临时文件并记录
                await safe_unlink(tmp_path)
                if attempt > 0:
                    logger.info(
                        f"[RangeDownload] 分块续传成功: {file_path.name} part_{part_idx}（重试 {attempt} 次）"
                    )
                return True
            except TimeoutError:
                if attempt < 2:
                    wait = 1 + attempt
                    self.cfg.verbose(
                        f"[RangeDownload] 分块 {part_idx} 超时，{wait}s 后重试"
                    )
                    await sleep(wait)
                    continue
                return False
            except Exception as e:
                self.cfg.verbose(
                    f"[RangeDownload] 分块 {part_idx} 失败 | attempt={attempt + 1} | reason={e}"
                )
                if attempt < 2:
                    await sleep(1 + attempt)
                    continue
                return False
        return False

    async def _download_range(
        self,
        url: str,
        file_path: Path,
        total_size: int,
        headers: dict[str, str],
        proxy: str | None,
        platform: str | None,
    ) -> bool:
        """执行分块下载，失败返回 False 让上层回退单连接。

        支持四种场景：
        - 单块下载中断用 Range 续传
        - 部分块已完成跳过
        - 合并时中断重新合并不重新下载块
        - 服务器不支持 Range 时回退到完整下载
        """
        parts = self._effective_max_parts(platform, total_size)
        if parts <= 1 or self._range_disabled(platform):
            return False

        # 预分配文件
        try:
            async with aiofiles.open(file_path, "wb") as f:
                await f.seek(total_size - 1)
                await f.write(b"\x00")
        except Exception as e:
            logger.warning(f"[RangeDownload] 预分配文件失败: {e}")
            return False

        chunk_size = total_size // parts
        ranges: list[tuple[int, int]] = []
        for i in range(parts):
            s = i * chunk_size
            e = total_size - 1 if i == parts - 1 else (i + 1) * chunk_size - 1
            ranges.append((s, e))

        self.cfg.verbose(
            f"[RangeDownload] 开始分块下载 | file={file_path.name} | parts={parts} | "
            f"size={total_size / 1024 / 1024:.2f}MB | platform={platform or '?'}"
        )

        try:
            async with asyncio.timeout(self.cfg.range_total_timeout):
                results = await gather(
                    *[
                        self._download_part(
                            url,
                            file_path,
                            s,
                            e,
                            headers,
                            proxy,
                            platform,
                            i,
                        )
                        for i, (s, e) in enumerate(ranges)
                    ]
                )
            if all(results):
                # 清理可能残留的临时分块文件
                for i in range(parts):
                    await safe_unlink(file_path.with_name(f"{file_path.name}.part_{i}.tmp"))
                self._clear_range_failure(platform)
                self.cfg.verbose(f"[RangeDownload] 分块下载成功 | file={file_path.name}")
                return True
        except asyncio.TimeoutError:
            logger.warning("[RangeDownload] 整体下载超时，回退单连接")

        # 失败处理：保留临时文件以便下次续传
        self._record_range_failure(platform)
        return False

    async def cleanup_stale_range_temp_files(self, max_age_seconds: int = 86400) -> int:
        """清理超过指定时间的过期分块临时文件

        Args:
            max_age_seconds: 临时文件最大存活时间（秒），默认 24 小时

        Returns:
            清理的文件数量
        """
        if not self.cfg.cache_dir.exists():
            return 0
        now = time.time()
        cleaned = 0
        for tmp in self.cfg.cache_dir.glob("*.part_*.tmp"):
            try:
                age = now - tmp.stat().st_mtime
                if age > max_age_seconds:
                    days = age / 86400
                    logger.info(
                        f"[RangeDownload] 清理过期临时文件: {tmp.name}（已存在 {days:.1f} 天）"
                    )
                    await safe_unlink(tmp)
                    cleaned += 1
            except Exception as e:
                self.cfg.verbose(f"[RangeDownload] 清理临时文件 {tmp.name} 失败: {e}")
        if cleaned:
            logger.info(
                f"[RangeDownload] 临时文件清理完成，共清理 {cleaned} 个"
            )
        return cleaned

    # ---------- 单连接流式下载 ----------

    async def _download_single(
        self,
        url: str,
        file_path: Path,
        platform: Optional[str] = None,
        max_retries: int = 3,
        timeout: int = 30,
        cdn_fallback_urls: Optional[List[str]] = None,
        proxy: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Path:
        """单连接流式下载，支持重试、CDN 节点切换、限流检测"""
        # 快手平台重试次数 +1
        if platform == "kuaishou":
            max_retries = max(max_retries, 4)

        start_time = time.time()
        start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
        safe_url = sanitize_url(url)

        # 合并请求头
        final_headers = self.default_headers.copy()
        final_headers.update(self._get_headers(platform))
        if headers:
            final_headers.update(headers)

        # 构造候选节点列表
        all_nodes: list[str] = [url]
        if cdn_fallback_urls:
            for u in cdn_fallback_urls:
                if u and u not in all_nodes:
                    all_nodes.append(u)

        tried_nodes: list[str] = []
        current_node_idx = 0
        last_exc: Exception | None = None

        host = self._extract_host(all_nodes[0])
        self.cfg.verbose(
            f"[Download] 开始下载 | file={file_path.name} | node={host} | start={start_ts} | url={safe_url}"
        )

        async with self._semaphore:
            async with self.adaptive.acquire(platform):
                # 是否有备选节点（影响是否进行测速/限流切换）
                has_fallback = len(all_nodes) > 1

                while current_node_idx < len(all_nodes):
                    current_url = all_nodes[current_node_idx]
                    current_host = self._extract_host(current_url)
                    tried_nodes.append(current_host)

                    # 节点快速测速（仅当存在备选节点时执行，否则直接使用原节点）
                    if has_fallback:
                        speed_mbps = await self._test_node_speed(current_url, final_headers, proxy)
                        if 0 < speed_mbps < 0.5:
                            if current_node_idx + 1 >= len(all_nodes):
                                raise DownloadException(
                                    f"主节点 {current_host} 限流（速度 {speed_mbps:.1f}MB/s），"
                                    f"且无备选 CDN 节点可切换"
                                )
                            logger.warning(
                                f"Download 节点 {current_host} 限流，切换至下一个 CDN（速度：{speed_mbps:.1f}MB/s）"
                            )
                            current_node_idx += 1
                            continue

                    for attempt in range(max_retries + 1):
                        try:
                            async with self.client.get(
                                current_url,
                                headers=final_headers,
                                allow_redirects=True,
                                proxy=proxy,
                                timeout=ClientTimeout(total=timeout),
                            ) as response:
                                current_host = self._extract_host(str(response.url))

                                # 403/429 立即切换节点
                                if response.status in (403, 429):
                                    logger.warning(
                                        f"Download 节点 {current_host} 返回 {response.status}，切换至下一个 CDN"
                                    )
                                    break  # 跳出当前节点重试，切换下一个节点

                                if response.status >= 400:
                                    raise ClientError(f"HTTP {response.status} {response.reason}")

                                content_length = response.content_length
                                max_bytes = self.max_size * 1024 * 1024

                                if content_length == 0:
                                    logger.warning(f"媒体 url: {sanitize_url(current_url)}, 大小为 0, 取消下载")
                                    raise ZeroSizeException
                                if content_length and content_length > max_bytes:
                                    logger.warning(
                                        f"媒体 url: {sanitize_url(current_url)} 大小 {content_length / 1024 / 1024:.2f} MB 超过 {self.max_size} MB, 取消下载"
                                    )
                                    raise SizeLimitException

                                downloaded = 0
                                with self.get_progress_bar(file_path.name, content_length) as bar:
                                    async with aiofiles.open(file_path, "wb") as file:
                                        async for chunk in response.content.iter_chunked(1024 * 1024):
                                            downloaded += len(chunk)
                                            if downloaded > max_bytes:
                                                raise SizeLimitException
                                            await file.write(chunk)
                                            bar.update(len(chunk))

                                if downloaded == 0:
                                    logger.warning(f"媒体 url: {sanitize_url(current_url)}, 实际大小为 0, 取消下载")
                                    raise ZeroSizeException
                                if content_length and downloaded < content_length:
                                    raise ClientError(
                                        f"HTTP payload incomplete {downloaded}/{content_length}"
                                    )

                            elapsed = time.time() - start_time
                            file_size = file_path.stat().st_size
                            retry_count = len(tried_nodes) - 1 + attempt
                            logger.info(
                                f"Download 下载成功 重试 {retry_count} 次后 file={file_path.name} "
                                f"node={current_host} size={file_size / 1024 / 1024:.2f}MB"
                            )
                            await self.adaptive.report_success(platform)
                            await self._record_download_result(True)
                            return file_path

                        except (ZeroSizeException, SizeLimitException):
                            await safe_unlink(file_path)
                            await self.adaptive.report_failure(platform)
                            await self._record_download_result(False)
                            raise

                        except (ClientError, TimeoutError) as exc:
                            await safe_unlink(file_path)
                            last_exc = exc
                            error_type = self._classify_download_error(exc)

                            # 限流错误直接切换节点，不等待重试
                            if error_type in ERROR_RATE_LIMIT or (isinstance(exc, ClientError) and "429" in str(exc)):
                                logger.warning(
                                    f"Download 节点 {current_host} 限流，切换至下一个 CDN"
                                )
                                break

                            if attempt < max_retries:
                                # 计算等待时间
                                if not self.cfg.download_retry_wait_enabled:
                                    self.cfg.verbose(
                                        f"Download 下载失败，立即重试（等待已关闭）file={file_path.name}"
                                    )
                                    wait = 0.0
                                else:
                                    if error_type in ERROR_NETWORK_JITTER:
                                        base = self.cfg.download_retry_delay_base
                                    else:
                                        base = 0.5
                                    wait = min(base * (2 ** attempt), self.cfg.download_retry_delay_limit)
                                    logger.warning(
                                        f"Download 下载失败，等待 {wait:.1f}s 后重试 第 {attempt + 1} 次 "
                                        f"file={file_path.name} node={current_host} error={exc}"
                                    )
                                if wait > 0:
                                    await sleep(wait)
                                continue

                            # 当前节点最终失败
                            logger.warning(
                                f"Download 节点 {current_host} 失败，切换至下一个 CDN"
                            )
                            break

                    current_node_idx += 1

        # 所有节点都失败
        await self._record_download_result(False)
        await self.adaptive.report_failure(platform)
        nodes_str = ",".join(tried_nodes)
        err_msg = f"Download 下载最终失败 file={file_path.name} nodes={nodes_str} error={last_exc}"
        logger.error(err_msg)
        logger.exception(f"下载失败 | url: {safe_url}, file_path: {file_path}")
        raise DownloadException("媒体下载失败") from last_exc

    # ---------- 批量下载 ----------

    async def download_batch(
        self,
        urls: list[str],
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        platform: str | None = None,
    ) -> list[Path]:
        """分批次下载同一 CDN 上的多个小图片，批次间有延迟。

        单张图片下载失败时记录到日志并继续处理后续图片，不中断整体流程。
        返回结果只包含成功下载的 Path。
        """
        if not urls:
            return []

        results: list[Path] = []
        failed_urls: list[tuple[str, BaseException]] = []
        batch_size = max(1, int(self.cfg.download_batch_size))
        total_batches = (len(urls) + batch_size - 1) // batch_size
        for batch_idx, i in enumerate(range(0, len(urls), batch_size), start=1):
            batch = urls[i : i + batch_size]
            self.cfg.verbose(
                f"[DownloadBatch] 处理批次 {batch_idx}/{total_batches} | size={len(batch)} | platform={platform or '?'}"
            )
            tasks = [
                self.download_img(
                    url,
                    headers=headers,
                    proxy=proxy,
                    platform=platform,
                )
                for url in batch
            ]
            batch_results = await gather(*tasks, return_exceptions=True)
            for url, res in zip(batch, batch_results):
                if isinstance(res, Path):
                    results.append(res)
                elif isinstance(res, BaseException):
                    failed_urls.append((url, res))
                    logger.warning(
                        f"[DownloadBatch] 单张图片下载失败 | url={sanitize_url(url)} | "
                        f"platform={platform or '?'} | reason={res}"
                    )
                else:
                    failed_urls.append((url, RuntimeError(f"unexpected result: {res!r}")))
                    logger.warning(
                        f"[DownloadBatch] 单张图片返回异常 | url={sanitize_url(url)} | result={res!r}"
                    )
            # 批次间延迟，避免对 CDN 造成压力
            if i + batch_size < len(urls):
                await sleep(0.2)

        if failed_urls:
            logger.warning(
                f"[DownloadBatch] 整体完成 | 成功={len(results)}/{len(urls)} | "
                f"失败={len(failed_urls)} | platform={platform or '?'}"
            )
        else:
            self.cfg.verbose(
                f"[DownloadBatch] 整体完成 | 全部成功 {len(results)}/{len(urls)} | platform={platform or '?'}"
            )

        return results

    # ---------- 请求间随机延迟 ----------

    async def _post_request_delay(self) -> None:
        """每个请求完成后随机延迟"""
        lo = max(0, float(self.cfg.download_delay_min))
        hi = max(lo, float(self.cfg.download_delay_max))
        if hi > 0:
            await sleep(random.uniform(lo, hi))

    # ---------- 统一优化下载入口 ----------

    async def _download_with_optimizations(
        self,
        url: str,
        file_path: Path,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        platform: str | None = None,
        candidates: list[str] | None = None,
    ) -> Path:
        """智能选择 CDN 优选 / 分块下载 / 单连接下载"""
        final_headers = self.default_headers.copy()
        final_headers.update(self._get_headers(platform))
        if headers:
            final_headers.update(headers)

        # 1. CDN 优选（需要多个候选 URL）
        urls = [url] + (candidates or [])
        urls = [u for u in urls if u]
        if self.cfg.cdn_prefetch_enabled and len(urls) > 1:
            url, _ = await self._select_best_cdn(urls, final_headers, proxy)

        # 2. 探测文件大小
        total_size = await self._get_file_size(url, final_headers, proxy)
        threshold = self._smart_threshold(total_size)

        # 3. 小文件直接单连接
        if not threshold["range"]:
            self.cfg.verbose(
                f"[Download] 文件小于10MB，跳过所有优化 | file={file_path.name}"
            )
            return await self._download_single(
                url, file_path, platform=platform, cdn_fallback_urls=candidates, proxy=proxy, headers=headers
            )

        # 4. 尝试分块下载（压缩开启时用户开关无效，因为压缩关闭才生效）
        if (
            self.cfg.enable_range_download
            and not self.cfg.video_compress_enable
            and total_size
            and threshold["range"]
        ):
            if await self._download_range(url, file_path, total_size, final_headers, proxy, platform):
                await self._post_request_delay()
                return file_path
            logger.warning("[Download] 分块下载失败，回退到单连接下载")

        result = await self._download_single(
            url, file_path, platform=platform, cdn_fallback_urls=candidates, proxy=proxy, headers=headers
        )
        await self._post_request_delay()
        return result

    # ---------- 压缩相关 ----------

    def _resolve_compress_params(self) -> dict[str, Any]:
        """解析当前配置下的压缩参数"""
        mode = (self.cfg.video_compress_quality_mode or "balance").lower()
        user_encoder = (self.cfg.video_compress_encoder or "auto").lower()
        encoder_name = ENCODER_NAME_MAP.get(user_encoder, "auto")
        # 这里不处理硬件检测回退，假设 compressor 会处理
        if encoder_name == "auto":
            encoder_name = "libx264"

        quality = QUALITY_PRESETS.get(mode, QUALITY_PRESETS["balance"]).get(encoder_name, 23)
        cfg = ENCODER_CONFIG.get(encoder_name, ENCODER_CONFIG["libx264"])

        params: dict[str, Any] = {
            "encoder": encoder_name,
            "quality_param": cfg["quality_param"],
            "quality_value": quality,
        }

        # preset
        preset = None
        if mode == "custom":
            preset = self.cfg.video_compress_custom_preset or "medium"
        else:
            preset_map = {"quality": "slow", "balance": "medium", "speed": "veryfast"}
            preset = preset_map.get(mode, "medium")

        from .compress import PRESET_MAP
        params["preset"] = PRESET_MAP.get(preset, {}).get(encoder_name)

        # resolution
        resolution = None
        if mode == "custom":
            res_cfg = self.cfg.video_compress_custom_resolution or "original"
            resolution = RESOLUTION_MAP.get(res_cfg, res_cfg if "x" in res_cfg else None)
        params["resolution"] = resolution

        # fps
        fps = None
        if mode == "custom":
            fps_cfg = self.cfg.video_compress_custom_fps or "original"
            if fps_cfg != "original":
                fps = fps_cfg.replace("fps", "")
        params["fps"] = fps

        # audio bitrate
        audio_bitrate = "128k"
        if mode == "custom":
            audio_bitrate = self.cfg.video_compress_custom_audio_bitrate or "128k"
        params["audio_bitrate"] = audio_bitrate

        # threads
        threads = None
        if mode == "custom":
            threads = self.cfg.video_compress_custom_threads
        params["threads"] = threads

        return params

    def _should_compress(self, size: int | None) -> bool:
        """根据配置和文件大小决定是否压缩"""
        if not self.cfg.video_compress_enable:
            return False
        if size is None:
            return True
        if size < SMALL_FILE_THRESHOLD:
            return False
        if size < NO_COMPRESS_THRESHOLD:
            return False
        return True

    async def _stream_compress(
        self,
        input_path: Path,
        output_path: Path,
    ) -> Path:
        """传统文件到文件压缩（作为流式压缩失败回退）

        命令显式声明 ``-f mp4``，强制 FFmpeg 将输出封装为 mp4 容器，
        避免因输出扩展名与探测到的 muxer 不一致而抛
        ``Unable to find a suitable output format``。
        """
        params = self._resolve_compress_params()
        encoder = params["encoder"]
        quality_param = params["quality_param"]
        quality_value = params["quality_value"]

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-c:v",
            encoder,
        ]
        if quality_param:
            cmd.extend([quality_param, str(quality_value)])
        if params.get("preset"):
            preset_param = ENCODER_CONFIG[encoder].get("preset_param")
            if preset_param:
                cmd.extend([preset_param, params["preset"]])
        if params.get("resolution"):
            cmd.extend(["-vf", f"scale={params['resolution']}"])
        if params.get("fps"):
            cmd.extend(["-r", str(params["fps"])])
        if params.get("threads") is not None and encoder == "libx264":
            cmd.extend(["-threads", str(params["threads"])])
        cmd.extend([
            "-c:a",
            "aac",
            "-b:a",
            params["audio_bitrate"],
            "-f",
            "mp4",
            "-movflags",
            "+faststart",
            str(output_path),
        ])

        from .utils import exec_ffmpeg_cmd

        await exec_ffmpeg_cmd(cmd)
        return output_path

    async def _compress_video(self, input_path: Path) -> Path:
        """压缩视频，先尝试流式压缩（如启用），失败回退传统压缩

        输出文件统一使用 ``.mp4`` 扩展名，配合 ffmpeg 命令中的 ``-f mp4``
        显式声明容器，避免某些场景下 FFmpeg 报
        ``Unable to find a suitable output format``。

        调用方应仅在音视频合并完成后调用本方法，每个视频只压缩一次。
        """
        output_path = input_path.with_name(f"{input_path.stem}_compressed.mp4")
        if output_path.exists():
            return output_path

        import time as _t

        # 当前实现：流式压缩与回退都走同一个文件到文件的命令。
        # 保留 enable_streaming_compress 开关以兼容未来真正的流式压缩实现。
        if not self.cfg.enable_streaming_compress:
            logger.info(
                f"[VideoCompress] 开始压缩（合并后） | input={input_path.name} | "
                f"output={output_path.name}"
            )
            start = _t.time()
            result = await self._stream_compress(input_path, output_path)
            logger.info(
                f"[VideoCompress] 压缩完成，耗时 {_t.time() - start:.2f}s"
            )
            return result

        logger.info(
            f"[VideoCompress] 开始压缩（合并后） | input={input_path.name} | "
            f"output={output_path.name}"
        )
        start = _t.time()
        params = self._resolve_compress_params()
        encoder = params.get("encoder", "libx264")
        try:
            result = await self._stream_compress(input_path, output_path)
            logger.info(
                f"[VideoCompress] 压缩完成，耗时 {_t.time() - start:.2f}s"
            )
            return result
        except Exception as e:
            logger.warning(f"[VideoCompress] 流式压缩失败，回退传统压缩: {e}")
            await safe_unlink(output_path)
            start = _t.time()
            result = await self._stream_compress(input_path, output_path)
            logger.info(
                f"[VideoCompress] 压缩完成，耗时 {_t.time() - start:.2f}s"
            )
            return result

    # ---------- 视频缓存 ----------

    def _video_cache_key(
        self,
        url: str,
        size: int | None,
        resolution: str | None,
    ) -> str:
        params = self._resolve_compress_params()
        quality_mode = self.cfg.video_compress_quality_mode or "unknown"
        quality_value = params.get("quality_value", "unknown")
        return VideoCacheManager.compute_key(
            url, size or 0, resolution, quality_mode, quality_value
        )

    def _get_cached_video(
        self,
        url: str,
        size: int | None,
        resolution: str | None,
    ) -> Path | None:
        if not self.cfg.video_cache_enabled or not self.video_cache:
            return None
        key = self._video_cache_key(url, size, resolution)
        path = self.video_cache.get(key)
        if path:
            logger.info(f"[VideoCache] 命中缓存 | key={key}")
        return path

    def _set_cached_video(
        self,
        url: str,
        size: int | None,
        resolution: str | None,
        path: Path,
    ) -> Path:
        if not self.cfg.video_cache_enabled or not self.video_cache:
            return path
        key = self._video_cache_key(url, size, resolution)
        return self.video_cache.set(key, path)

    # ---------- 公开下载接口 ----------

    @auto_task
    async def streamd(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        platform: str | None = None,
        candidates: list[str] | None = None,
    ) -> Path:
        """流式下载（支持 CDN 优选、分块下载）

        Args:
            platform: 平台标识，用于自适应并发控制。留空则不启用平台级限流。
            candidates: 备选 URL 列表，用于 CDN 优选。
        """
        if not file_name:
            file_name = generate_file_name(url)
        file_path = self.cfg.cache_dir / file_name
        if file_path.exists():
            return file_path

        # proxy 默认值处理
        if proxy is ...:
            proxy = self.cfg.proxy
        elif proxy is None:
            proxy = None

        return await self._download_with_optimizations(
            url, file_path, headers, proxy, platform, candidates
        )

    @auto_task
    async def download_video(
        self,
        url: str,
        *,
        video_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        platform: str | None = None,
        candidates: list[str] | None = None,
        original_url: str | None = None,
        resolution: str | None = None,
    ) -> Path:
        """下载视频，支持缓存命中、CDN 优选、分块下载、压缩、缓存存储"""
        if video_name is None:
            video_name = generate_file_name(url, ".mp4")
        video_path = self.cfg.cache_dir / video_name

        # 视频缓存检查
        cache_url = original_url or url
        if cached := self._get_cached_video(cache_url, None, resolution):
            return cached

        # 先下载到临时路径
        tmp_path = await self.streamd(
            url,
            file_name=video_name,
            headers=headers,
            proxy=proxy,
            platform=platform,
            candidates=candidates,
        )

        size = tmp_path.stat().st_size if tmp_path.exists() else None

        # 注意：单视频下载阶段不再触发压缩，压缩仅在音视频合并完成后执行一次，
        # 避免 download_av_and_merge 与 sender 重复压缩导致耗时翻倍。

        # 缓存结果
        return self._set_cached_video(cache_url, size, resolution, tmp_path)

    @auto_task
    async def download_audio(
        self,
        url: str,
        *,
        audio_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        platform: str | None = None,
        candidates: list[str] | None = None,
    ) -> Path:
        if audio_name is None:
            audio_name = generate_file_name(url, ".mp3")
        return await self.streamd(
            url,
            file_name=audio_name,
            headers=headers,
            proxy=proxy,
            platform=platform,
            candidates=candidates,
        )

    @auto_task
    async def download_file(
        self,
        url: str,
        *,
        file_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        platform: str | None = None,
        candidates: list[str] | None = None,
    ) -> Path:
        if file_name is None:
            file_name = generate_file_name(url, ".zip")
        return await self.streamd(
            url, file_name=file_name, headers=headers, proxy=proxy, platform=platform, candidates=candidates
        )

    @auto_task
    async def download_img(
        self,
        url: str,
        *,
        img_name: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        platform: str | None = None,
        candidates: list[str] | None = None,
    ) -> Path:
        if img_name is None:
            img_name = generate_file_name(url, ".jpg")
        return await self.streamd(
            url, file_name=img_name, headers=headers, proxy=proxy, platform=platform, candidates=candidates
        )

    async def download_imgs_without_raise(
        self,
        urls: list[str],
        *,
        headers: dict[str, str] | None = None,
        proxy: str | None | object = ...,
        platform: str | None = None,
    ) -> list[Path]:
        paths_or_errs = await gather(
            *[
                self.download_img(url, headers=headers, proxy=proxy, platform=platform)
                for url in urls
            ],
            return_exceptions=True,
        )
        return [p for p in paths_or_errs if isinstance(p, Path)]

    # ---------- 音视频合并 ----------

    async def _merge_av_adaptive(
        self,
        v_path: Path,
        a_path: Path,
        output_path: Path,
    ) -> Path:
        """根据配置和文件大小选择内存合并或流式合并"""
        v_size = v_path.stat().st_size if v_path.exists() else 0
        a_size = a_path.stat().st_size if a_path.exists() else 0
        total_size = v_size + a_size
        threshold = self.cfg.range_merge_threshold_mb * 1024 * 1024
        # 将 video_merge_faststart 配置透传到合并函数；缺失时默认开启
        faststart = bool(getattr(self.cfg, "video_merge_faststart", True))

        # 内存自适应模式
        if self.cfg.range_memory_adaptive:
            mem = memory_info()
            if mem is not None:
                total_mem, available = mem
                reserve = self.cfg.range_memory_reserve_percent / 100
                usable = available * (1 - reserve)
                if total_size < usable * 0.5:
                    await merge_av(
                        v_path=v_path, a_path=a_path, output_path=output_path,
                        faststart=faststart,
                    )
                    return output_path
                elif total_size < usable * 0.8:
                    # 内存合并但降低分块数（由后续下载逻辑处理）
                    await merge_av(
                        v_path=v_path, a_path=a_path, output_path=output_path,
                        faststart=faststart,
                    )
                    return output_path
                else:
                    await merge_av_streaming(
                        v_path=v_path, a_path=a_path, output_path=output_path,
                        faststart=faststart,
                    )
                    return output_path

        if total_size < threshold:
            await merge_av(
                v_path=v_path, a_path=a_path, output_path=output_path,
                faststart=faststart,
            )
        else:
            await merge_av_streaming(
                v_path=v_path, a_path=a_path, output_path=output_path,
                faststart=faststart,
            )
        return output_path

    @auto_task
    async def download_av_and_merge(
        self,
        v_url: str,
        a_url: str,
        *,
        output_path: Path,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        platform: str | None = None,
        original_url: str | None = None,
        resolution: str | None = None,
    ) -> Path:
        """download video and audio file by url with stream and merge，合并后支持压缩/缓存"""
        # 检查缓存
        cache_url = original_url or f"{v_url}|{a_url}"
        if cached := self._get_cached_video(cache_url, None, resolution):
            return cached

        v_path, a_path = await gather(
            self.download_video(v_url, headers=headers, proxy=proxy, platform=platform),
            self.download_audio(a_url, headers=headers, proxy=proxy, platform=platform),
            return_exceptions=True,
        )
        # 失败隔离：任一失败则抛出
        if isinstance(v_path, Exception):
            raise DownloadException("视频下载失败") from v_path
        if isinstance(a_path, Exception):
            raise DownloadException("音频下载失败") from a_path

        await self._merge_av_adaptive(v_path=v_path, a_path=a_path, output_path=output_path)

        size = output_path.stat().st_size if output_path.exists() else None
        final_path = output_path
        if self._should_compress(size):
            try:
                final_path = await self._compress_video(output_path)
            except Exception as e:
                logger.error(f"[Download] 合并后压缩失败，使用原文件: {e}")
                final_path = output_path

        return self._set_cached_video(cache_url, size, resolution, final_path)

    # ---------- yt-dlp 相关 ----------

    async def ytdlp_extract_info(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> VideoInfo:
        if (info := self.info_cache.get(url)) is not None:
            return info
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not raw:
                raise ParseException("获取视频信息失败")
        info = convert(raw, VideoInfo)
        self.info_cache[url] = info
        return info

    async def ytdlp_extract_raw(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
    ) -> dict[str, Any]:
        opts = {
            "quiet": True,
            "skip_download": True,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if format:
            opts["format"] = format

        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            raw = await to_thread(ydl.extract_info, url, download=False)
            if not isinstance(raw, dict):
                raise ParseException("yt-dlp 返回数据异常")
            return raw  # type: ignore

    async def _ytdlp_select_best_format(
        self,
        info: dict[str, Any],
        headers: dict[str, str],
        proxy: str | None,
    ) -> str | None:
        """对 yt-dlp formats 中的视频 URL 进行 CDN 测速，返回最佳 format id"""
        if not self.cfg.cdn_prefetch_enabled:
            return None
        formats = info.get("formats") or []
        video_formats = [f for f in formats if f.get("vcodec") != "none" and f.get("url")]
        if len(video_formats) <= 1:
            return None

        # 选取前几个不同 format_id
        urls = []
        format_ids = []
        seen = set()
        for f in video_formats:
            fid = f.get("format_id")
            u = f.get("url")
            if fid and u and fid not in seen:
                seen.add(fid)
                urls.append(u)
                format_ids.append(fid)
            if len(urls) >= 4:
                break
        if len(urls) <= 1:
            return None

        best_url, _ = await self._select_best_cdn(urls, headers, proxy)
        idx = urls.index(best_url)
        best_fid = format_ids[idx]
        self.cfg.verbose(f"[CDN] yt-dlp 选择 format_id={best_fid}")
        return best_fid

    @auto_task
    async def ytdlp_download_video(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
        platform: str | None = None,
        original_url: str | None = None,
        resolution: str | None = None,
    ) -> Path:
        info = await self.ytdlp_extract_info(
            url, cookiefile=cookiefile, headers=headers, proxy=proxy
        )
        if info.duration > self.cfg.max_duration:
            raise DurationLimitException

        video_path = self.cfg.cache_dir / generate_file_name(url, ".mp4")
        if video_path.exists():
            return video_path

        # 检查缓存
        cache_url = original_url or url
        if cached := self._get_cached_video(cache_url, None, resolution):
            return cached

        # CDN 优选：尝试从 formats 中选择最佳 format_id
        selected_format = format
        if self.cfg.cdn_prefetch_enabled and not selected_format:
            try:
                raw_info = await self.ytdlp_extract_raw(
                    url, cookiefile=cookiefile, headers=headers, proxy=proxy
                )
                best_fid = await self._ytdlp_select_best_format(
                    raw_info, headers or self.default_headers, proxy
                )
                if best_fid:
                    selected_format = best_fid
            except Exception as e:
                self.cfg.verbose(f"[CDN] yt-dlp format 测速失败: {e}")

        opts = {
            "outtmpl": str(video_path),
            "merge_output_format": "mp4",
            "format": selected_format or "best",
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}

        async with self._semaphore:
            async with self.adaptive.acquire(platform):
                start_time = time.time()
                start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
                host = self._extract_host(url)
                safe_url = sanitize_url(url)
                self.cfg.verbose(
                    f"[Download] 开始 yt-dlp 下载 | file={video_path.name} | node={host} | start={start_ts} | url={safe_url}"
                )
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
                        await to_thread(ydl.download, [url])
                    elapsed = time.time() - start_time
                    file_size = video_path.stat().st_size if video_path.exists() else 0
                    self.cfg.verbose(
                        f"[Download] yt-dlp 下载成功 | file={video_path.name} | node={host} | "
                        f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                    )
                    await self.adaptive.report_success(platform)
                    await self._record_download_result(True)
                except Exception as exc:
                    await self.adaptive.report_failure(platform)
                    await self._record_download_result(False)
                    error_type = self._classify_download_error(exc)
                    self.cfg.verbose(
                        f"[Download] yt-dlp 下载失败 | file={video_path.name} | node={host} | "
                        f"error_type={error_type} | reason={exc}"
                    )
                    raise

        await self._post_request_delay()

        # 压缩与缓存
        size = video_path.stat().st_size if video_path.exists() else None

        # yt-dlp 视频下载阶段不再触发压缩；如需压缩，请走 download_av_and_merge 合并后由其执行。
        return self._set_cached_video(cache_url, size, resolution, video_path)

    @auto_task
    async def ytdlp_download_video_relaxed(
        self,
        url: str,
        *,
        cookiefile: Path | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        node: bool = False,
        platform: str | None = None,
        original_url: str | None = None,
        resolution: str | None = None,
    ) -> Path:
        file_stem = generate_file_name(url)
        video_path = self.cfg.cache_dir / f"{file_stem}.mp4"
        if video_path.exists():
            return video_path

        cache_url = original_url or url
        if cached := self._get_cached_video(cache_url, None, resolution):
            return cached

        opts = {
            "outtmpl": str(self.cfg.cache_dir / file_stem) + ".%(ext)s",
            "merge_output_format": "mp4",
            "format": format or None,
            "postprocessors": [
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}
            ],
            "http_headers": headers or self.default_headers,
            "quiet": True,
            "no_warnings": True,
        }
        if not opts["format"]:
            opts.pop("format")
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)
        if node:
            opts["js_runtimes"] = {"node": {}}

        async with self._semaphore:
            async with self.adaptive.acquire(platform):
                start_time = time.time()
                start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
                host = self._extract_host(url)
                safe_url = sanitize_url(url)
                self.cfg.verbose(
                    f"[Download] 开始 yt-dlp 宽松下载 | file={video_path.name} | node={host} | start={start_ts} | url={safe_url}"
                )
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
                        await to_thread(ydl.download, [url])
                    if video_path.exists():
                        elapsed = time.time() - start_time
                        file_size = video_path.stat().st_size
                        self.cfg.verbose(
                            f"[Download] yt-dlp 宽松下载成功 | file={video_path.name} | node={host} | "
                            f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                        )
                        await self.adaptive.report_success(platform)
                        await self._record_download_result(True)
                    else:
                        candidates = sorted(self.cfg.cache_dir.glob(f"{file_stem}*.mp4"))
                        if candidates:
                            video_path = candidates[0]
                            elapsed = time.time() - start_time
                            file_size = video_path.stat().st_size
                            self.cfg.verbose(
                                f"[Download] yt-dlp 宽松下载成功（候选文件） | file={video_path.name} | node={host} | "
                                f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                            )
                            await self.adaptive.report_success(platform)
                            await self._record_download_result(True)
                        else:
                            await self.adaptive.report_failure(platform)
                            await self._record_download_result(False)
                            raise DownloadException("yt-dlp 视频下载失败")
                except Exception as exc:
                    await self.adaptive.report_failure(platform)
                    await self._record_download_result(False)
                    error_type = self._classify_download_error(exc)
                    self.cfg.verbose(
                        f"[Download] yt-dlp 宽松下载失败 | file={video_path.name} | node={host} | "
                        f"error_type={error_type} | reason={exc}"
                    )
                    raise

        await self._post_request_delay()

        size = video_path.stat().st_size if video_path.exists() else None

        # yt-dlp 宽松模式下载阶段不再触发压缩；如需压缩，请走 download_av_and_merge 合并后由其执行。
        return self._set_cached_video(cache_url, size, resolution, video_path)

    @auto_task
    async def ytdlp_download_audio(
        self,
        url: str,
        *,
        cookiefile: Path | None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        format: str | None = None,
        platform: str | None = None,
    ) -> Path:
        file_name = generate_file_name(url)
        audio_path = self.cfg.cache_dir / f"{file_name}.flac"
        if audio_path.exists():
            return audio_path

        opts = {
            "outtmpl": str(self.cfg.cache_dir / file_name) + ".%(ext)s",
            "format": format or "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "flac",
                    "preferredquality": "0",
                }
            ],
            "cookiefile": None,
            "http_headers": headers or self.default_headers,
        }
        if proxy:
            opts["proxy"] = proxy
        if cookiefile and cookiefile.is_file():
            opts["cookiefile"] = str(cookiefile)

        async with self._semaphore:
            async with self.adaptive.acquire(platform):
                start_time = time.time()
                start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
                host = self._extract_host(url)
                safe_url = sanitize_url(url)
                self.cfg.verbose(
                    f"[Download] 开始 yt-dlp 音频下载 | file={audio_path.name} | node={host} | start={start_ts} | url={safe_url}"
                )
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
                        await to_thread(ydl.download, [url])
                    elapsed = time.time() - start_time
                    file_size = audio_path.stat().st_size if audio_path.exists() else 0
                    self.cfg.verbose(
                        f"[Download] yt-dlp 音频下载成功 | file={audio_path.name} | node={host} | "
                        f"elapsed={elapsed:.2f}s | size={file_size / 1024 / 1024:.2f}MB"
                    )
                    await self.adaptive.report_success(platform)
                    await self._record_download_result(True)
                    return audio_path
                except Exception as exc:
                    await self.adaptive.report_failure(platform)
                    await self._record_download_result(False)
                    error_type = self._classify_download_error(exc)
                    self.cfg.verbose(
                        f"[Download] yt-dlp 音频下载失败 | file={audio_path.name} | node={host} | "
                        f"error_type={error_type} | reason={exc}"
                    )
                    raise

    @staticmethod
    def get_progress_bar(desc: str, total: int | None = None) -> tqdm:
        """获取进度条 bar"""
        return tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            colour="green",
            desc=desc,
        )
