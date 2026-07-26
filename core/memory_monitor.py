"""内存监控模块

定期检测系统内存，当可用内存低于阈值时自动降低分块下载数。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger


class MemoryMonitor:
    """系统内存监控器"""

    def __init__(self, cfg: Any):
        self.cfg = cfg
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._memory_pressure = False
        self._has_psutil = False
        try:
            import psutil

            self._has_psutil = True
        except Exception:
            logger.warning("[MemoryMonitor] 未安装 psutil，内存监控功能不可用")

    @property
    def enabled(self) -> bool:
        node = getattr(self.cfg, "memory_monitor", None)
        if node is None:
            return False
        return bool(getattr(node, "enabled", False))

    @property
    def interval(self) -> int:
        node = getattr(self.cfg, "memory_monitor", None)
        if node is None:
            return 30
        return max(5, int(getattr(node, "interval", 30)))

    @property
    def warning_threshold(self) -> float:
        node = getattr(self.cfg, "memory_monitor", None)
        if node is None:
            return 0.1
        return max(0.01, min(0.95, float(getattr(node, "warning_threshold", 10)) / 100))

    def _mem_info(self) -> tuple[int, int] | None:
        """返回 (total_bytes, available_bytes)"""
        if not self._has_psutil:
            return None
        try:
            import psutil

            mem = psutil.virtual_memory()
            return mem.total, mem.available
        except Exception as e:
            logger.warning(f"[MemoryMonitor] 获取内存信息失败: {e}")
            return None

    def get_current_max_parts(self, user_max: int) -> int:
        """
        返回当前建议的最大分块数。
        内存充足时返回用户配置上限；内存不足时降低上限。
        """
        if not self.enabled or not self._has_psutil:
            return user_max
        if not self._memory_pressure:
            return user_max

        fallback_to_single = bool(
            getattr(self.cfg, "range_memory_fallback_to_single", False)
        )
        if fallback_to_single:
            return 1
        # 最低降到 2
        return max(2, min(user_max, 2))

    async def _check_once(self) -> None:
        """执行一次内存检测"""
        info = self._mem_info()
        if info is None:
            return
        total, available = info
        if total <= 0:
            return
        ratio = available / total
        was_pressure = self._memory_pressure

        if ratio < self.warning_threshold:
            self._memory_pressure = True
            if not was_pressure:
                logger.warning(
                    f"[MemoryMonitor] 内存压力警告 | 总内存={total / 1024 / 1024:.1f}MB | "
                    f"可用={available / 1024 / 1024:.1f}MB | 可用率={ratio * 100:.1f}% | "
                    f"已触发分块数降级"
                )
        else:
            self._memory_pressure = False
            if was_pressure:
                logger.info("[MemoryMonitor] 内存恢复充足，分块数上限已恢复")

    async def check_now(self) -> None:
        """立即执行一次内存检测（用于解析完成后主动恢复）"""
        if not self.enabled or not self._has_psutil:
            return
        try:
            await self._check_once()
        except Exception as e:
            logger.warning(f"[MemoryMonitor] 主动检测异常: {e}")

    async def _loop(self) -> None:
        """监控循环"""
        while not self._stop_event.is_set():
            try:
                await self._check_once()
            except Exception as e:
                logger.warning(f"[MemoryMonitor] 检测异常: {e}")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval
                )
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        """启动内存监控"""
        if not self.enabled:
            logger.debug("[MemoryMonitor] 内存监控未启用")
            return
        if not self._has_psutil:
            logger.warning("[MemoryMonitor] 未安装 psutil，无法启动内存监控")
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("[MemoryMonitor] 内存监控已启动")

    async def stop(self) -> None:
        """停止内存监控"""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
