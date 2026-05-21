"""LLM 熔断器模块 - 防止持续调用有问题的服务"""
import time
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import Callable, Any, Optional
import logging

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"      # 正常状态，可调用
    OPEN = "open"          # 熔断状态，拒绝调用
    HALF_OPEN = "half_open"  # 半开状态，尝试恢复


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5       # 连续失败次数阈值
    recovery_timeout: int = 60       # 熔断恢复超时（秒）
    half_open_max_calls: int = 3     # 半开状态允许的试探调用次数


class CircuitBreakerError(Exception):
    """熔断器拒绝请求时抛出此异常"""
    def __init__(self, provider: str, state: CircuitState, message: str = ""):
        self.provider = provider
        self.state = state
        self.message = message or f"Circuit breaker {state.value} for {provider}"
        super().__init__(self.message)


@dataclass
class CircuitBreaker:
    """
    熔断器实现 - 基于状态机模式

    状态转换:
    - CLOSED → OPEN: 连续失败达到阈值
    - OPEN → HALF_OPEN: 超过恢复超时
    - HALF_OPEN → CLOSED: 试探调用成功
    - HALF_OPEN → OPEN: 试探调用失败
    """
    name: str
    config: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    failure_count: int = field(default=0, init=False)
    success_count: int = field(default=0, init=False)
    half_open_calls: int = field(default=0, init=False)
    last_failure_time: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def call(self, func: Callable[[], Any], *args, **kwargs) -> Any:
        """
        通过熔断器执行函数调用

        Args:
            func: 要执行的函数
            *args, **kwargs: 函数参数

        Returns:
            函数返回值

        Raises:
            CircuitBreakerError: 熔断器处于 OPEN 状态时抛出
        """
        with self._lock:
            self._check_state_transition()

            if self.state == CircuitState.OPEN:
                raise CircuitBreakerError(
                    self.name,
                    self.state,
                    f"Circuit breaker is OPEN for {self.name}. Try again later."
                )

            # HALF_OPEN 状态下限制试探调用次数
            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.config.half_open_max_calls:
                    raise CircuitBreakerError(
                        self.name,
                        self.state,
                        f"Circuit breaker is HALF_OPEN for {self.name}. Max trial calls reached."
                    )
                self.half_open_calls += 1

        # 执行实际调用（锁外执行，避免阻塞其他线程）
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise e

    def _check_state_transition(self) -> None:
        """检查并执行状态转换"""
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time >= self.config.recovery_timeout:
                logger.info(f"Circuit breaker {self.name}: OPEN → HALF_OPEN")
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                self.failure_count = 0

    def _on_success(self) -> None:
        """记录成功调用"""
        with self._lock:
            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.config.half_open_max_calls:
                    logger.info(f"Circuit breaker {self.name}: HALF_OPEN → CLOSED")
                    self.state = CircuitState.CLOSED
                    self.failure_count = 0
                    self.success_count = 0
            else:
                # 成功时重置失败计数
                self.failure_count = 0

    def _on_failure(self) -> None:
        """记录失败调用"""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                logger.warning(f"Circuit breaker {self.name}: HALF_OPEN → OPEN (trial failed)")
                self.state = CircuitState.OPEN
                self.success_count = 0
            elif self.failure_count >= self.config.failure_threshold:
                logger.warning(
                    f"Circuit breaker {self.name}: CLOSED → OPEN "
                    f"(failures: {self.failure_count}/{self.config.failure_threshold})"
                )
                self.state = CircuitState.OPEN

    @property
    def is_available(self) -> bool:
        """检查熔断器是否可用（可接受请求）"""
        with self._lock:
            self._check_state_transition()
            return self.state != CircuitState.OPEN

    def reset(self) -> None:
        """手动重置熔断器"""
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.half_open_calls = 0
            self.last_failure_time = 0.0
            logger.info(f"Circuit breaker {self.name} has been reset")

    def get_status(self) -> dict:
        """获取熔断器状态"""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "threshold": self.config.failure_threshold,
                "recovery_timeout": self.config.recovery_timeout,
                "last_failure_time": self.last_failure_time,
            }


# 全局熔断器实例管理器
_circuit_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_circuit_breaker(
    name: str,
    config: Optional[CircuitBreakerConfig] = None
) -> CircuitBreaker:
    """获取或创建命名熔断器"""
    with _breakers_lock:
        if name not in _circuit_breakers:
            _circuit_breakers[name] = CircuitBreaker(
                name=name,
                config=config or CircuitBreakerConfig()
            )
        return _circuit_breakers[name]


def reset_all_circuit_breakers() -> None:
    """重置所有熔断器"""
    with _breakers_lock:
        for breaker in _circuit_breakers.values():
            breaker.reset()


def get_all_breaker_status() -> list[dict]:
    """获取所有熔断器状态"""
    with _breakers_lock:
        return [breaker.get_status() for breaker in _circuit_breakers.values()]
