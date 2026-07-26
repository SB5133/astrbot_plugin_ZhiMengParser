"""DNS 预解析模块

在插件启动时预解析所有已启用解析器对应的域名，支持定时刷新。
结果仅记录到日志，不影响聊天窗口。
"""

from __future__ import annotations

import asyncio
import re
import socket
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger

if TYPE_CHECKING:
    from .config import PluginConfig
    from .parsers import BaseParser


# 平台名称 -> 典型域名列表（用于 DNS 预解析）
PLATFORM_DOMAINS: dict[str, list[str]] = {
    "acfun": ["acfun.cn"],
    "bilibili": ["bilibili.com", "hdslb.com"],
    "douyin": ["douyin.com", "iesdouyin.com", "bytecdn.cn"],
    "instagram": ["instagram.com", "cdninstagram.com"],
    "kuaishou": ["kuaishou.com", "kwai.com"],
    "ncm": ["163.com", "126.net"],
    "nga": ["nga.cn", "178.com"],
    "tiktok": ["tiktok.com", "tiktokcdn.com"],
    "twitter": ["twitter.com", "x.com", "twimg.com"],
    "weibo": ["weibo.com", "weibo.cn", "sinaimg.cn"],
    "xiaoheihe": ["xiaoheihe.cn", "maxjia.com"],
    "zhihu": ["zhihu.com", "zhimg.com"],
    "xhs": ["xiaohongshu.com", "xhscdn.com"],
    "youtube": ["youtube.com", "youtu.be", "googlevideo.com"],
    "iwara": ["iwara.tv"],
    "shipinhao": ["channels.weixin.qq.com"],
}

# 从正则表达式字符串中尝试提取域名
_DOMAIN_RE = re.compile(r"https?://([^/\s]+)")


class DNSCacheManager:
    """DNS 预解析管理器"""

    def __init__(self, cfg: "PluginConfig"):
        self.cfg = cfg
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()

    @property
    def enabled(self) -> bool:
        node = getattr(self.cfg, "dns_prefetch", None)
        if node is None:
            return False
        return bool(getattr(node, "enabled", False))

    @property
    def prefetch_on_startup(self) -> bool:
        node = getattr(self.cfg, "dns_prefetch", None)
        if node is None:
            return False
        return bool(getattr(node, "prefetch_on_startup", False))

    @property
    def periodic_refresh(self) -> bool:
        node = getattr(self.cfg, "dns_prefetch", None)
        if node is None:
            return False
        return bool(getattr(node, "periodic_refresh", False))

    @property
    def refresh_interval(self) -> int:
        node = getattr(self.cfg, "dns_prefetch", None)
        if node is None:
            return 300
        return max(60, int(getattr(node, "refresh_interval", 300)))

    def _collect_domains(self, parser_classes: Sequence[type["BaseParser"]]) -> list[str]:
        """从已启用解析器中提取域名"""
        domains: set[str] = set()
        for cls in parser_classes:
            platform = cls.platform.name
            # 1. 使用平台预定义域名
            for d in PLATFORM_DOMAINS.get(platform, []):
                domains.add(d.lower())
            # 2. 尝试从 key_patterns 中提取域名
            for _, pattern in getattr(cls, "_key_patterns", []):
                for m in _DOMAIN_RE.finditer(pattern.pattern):
                    domains.add(m.group(1).lower())
        return sorted(domains)

    async def _resolve_one(self, domain: str, timeout: float = 2.0) -> tuple[str, list[str] | None]:
        """解析单个域名，返回 (domain, ips)"""
        try:
            ips = await asyncio.wait_for(
                asyncio.to_thread(socket.gethostbyname_ex, domain),
                timeout=timeout,
            )
            # gethostbyname_ex 返回 (hostname, aliaslist, ipaddrlist)
            return domain, ips[2] if ips else None
        except asyncio.TimeoutError:
            logger.warning(f"[DNSPrefetch] 解析 {domain} 超时")
            return domain, None
        except Exception as e:
            logger.warning(f"[DNSPrefetch] 解析 {domain} 失败: {e}")
            return domain, None

    async def prefetch(self, parser_classes: Sequence[type["BaseParser"]]) -> None:
        """执行一次 DNS 预解析"""
        domains = self._collect_domains(parser_classes)
        if not domains:
            logger.debug("[DNSPrefetch] 没有可预解析的域名")
            return

        logger.info(f"[DNSPrefetch] 开始预解析 {len(domains)} 个域名...")
        start = time.time()
        tasks = [self._resolve_one(d) for d in domains]
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=10.0,
        )

        ok = 0
        fail = 0
        for res in results:
            if isinstance(res, Exception):
                logger.warning(f"[DNSPrefetch] 解析异常: {res}")
                fail += 1
                continue
            domain, ips = res
            if ips:
                logger.debug(f"[DNSPrefetch] {domain} -> {ips[0]}")
                ok += 1
            else:
                fail += 1

        elapsed = time.time() - start
        logger.info(
            f"[DNSPrefetch] 预解析完成 | 成功={ok} | 失败={fail} | 耗时={elapsed:.2f}s"
        )

    async def _periodic_loop(self, parser_classes: Sequence[type["BaseParser"]]) -> None:
        """定时刷新循环"""
        while not self._stop_event.is_set():
            try:
                await self.prefetch(parser_classes)
            except Exception as e:
                logger.warning(f"[DNSPrefetch] 定时刷新异常: {e}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.refresh_interval
                )
            except asyncio.TimeoutError:
                continue

    async def start(self, parser_classes: Sequence[type["BaseParser"]]) -> None:
        """启动 DNS 预解析服务"""
        if not self.enabled:
            logger.debug("[DNSPrefetch] DNS 预解析总开关已关闭")
            return

        if self.prefetch_on_startup:
            try:
                await self.prefetch(parser_classes)
            except Exception as e:
                logger.warning(f"[DNSPrefetch] 启动预解析失败: {e}")

        if self.periodic_refresh:
            self._task = asyncio.create_task(self._periodic_loop(parser_classes))
            logger.info("[DNSPrefetch] 已启动定时刷新任务")

    async def stop(self) -> None:
        """停止定时刷新"""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
