"""动态并发控制器"""
import asyncio
import logging
import psutil
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)


class ConcurrencyController:
    """
    动态并发控制器 - 根据系统负载自动调整爬取并发数

    策略:
    - CPU 使用率 < 50%: 增加并发
    - CPU 使用率 > 80%: 减少并发
    - 内存使用率 > 85%: 暂停新增并发
    """

    def __init__(
        self,
        min_concurrent: int = 1,
        max_concurrent: int = 5,
        check_interval: float = 5.0,
    ):
        self.min_concurrent = min_concurrent
        self.max_concurrent = max_concurrent
        self.check_interval = check_interval
        self._current_concurrent = min_concurrent
        self._lock = asyncio.Lock()
        self._process = psutil.Process()
        self._last_logged_value = min_concurrent

    @property
    def semaphore_value(self) -> int:
        return self._current_concurrent

    async def check_and_adjust(self) -> int:
        """检查系统资源并调整并发数"""
        try:
            cpu_percent = self._process.cpu_percent(interval=0.1) / psutil.cpu_count()
            memory_percent = self._process.memory_percent()
        except Exception as e:
            logger.warning(f"Failed to get system resources: {e}")
            return self._current_concurrent

        async with self._lock:
            old_value = self._current_concurrent

            if memory_percent > 85:
                # 内存紧张，减少并发
                self._current_concurrent = max(self.min_concurrent, self._current_concurrent - 1)
                logger.debug(f"Memory high ({memory_percent:.1f}%), reducing concurrency to {self._current_concurrent}")
            elif cpu_percent > 80:
                # CPU 繁忙，减少并发
                self._current_concurrent = max(self.min_concurrent, self._current_concurrent - 1)
                logger.debug(f"CPU high ({cpu_percent:.1f}%), reducing concurrency to {self._current_concurrent}")
            elif cpu_percent < 50 and memory_percent < 70:
                # 资源充裕，增加并发
                self._current_concurrent = min(self.max_concurrent, self._current_concurrent + 1)
                logger.debug(f"Resources low (CPU {cpu_percent:.1f}%, Mem {memory_percent:.1f}%), increasing concurrency to {self._current_concurrent}")

            if self._current_concurrent != old_value and self._current_concurrent != self._last_logged_value:
                logger.info(f"Crawl concurrency adjusted: {old_value} -> {self._current_concurrent} (CPU {cpu_percent:.1f}%, Mem {memory_percent:.1f}%)")
                self._last_logged_value = self._current_concurrent

            return self._current_concurrent

    def get_semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(self._current_concurrent)


# Global instance
_controller: Optional[ConcurrencyController] = None


def get_concurrency_controller() -> ConcurrencyController:
    global _controller
    if _controller is None:
        _controller = ConcurrencyController(
            min_concurrent=settings.CRAWL_MIN_CONCURRENCY,
            max_concurrent=settings.CRAWL_MAX_CONCURRENCY,
        )
    return _controller