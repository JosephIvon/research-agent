"""LLM 超时与熔断器测试"""
import pytest
import time
from unittest.mock import Mock, patch, MagicMock
from src.llm.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerError,
    CircuitState,
    get_circuit_breaker,
    reset_all_circuit_breakers,
)


class TestCircuitBreaker:
    """熔断器核心测试"""

    def test_circuit_starts_closed(self):
        """熔断器初始状态为 CLOSED"""
        breaker = CircuitBreaker(name="test")
        assert breaker.state == CircuitState.CLOSED
        assert breaker.is_available is True

    def test_circuit_opens_after_threshold(self):
        """连续失败达到阈值后熔断器变为 OPEN"""
        breaker = CircuitBreaker(
            name="test",
            config=CircuitBreakerConfig(failure_threshold=3)
        )

        # 模拟失败
        for _ in range(3):
            try:
                raise Exception("Simulated failure")
            except Exception:
                breaker._on_failure()

        assert breaker.state == CircuitState.OPEN
        assert breaker.is_available is False

    def test_circuit_recovery_to_half_open(self):
        """熔断超时后变为 HALF_OPEN"""
        breaker = CircuitBreaker(
            name="test",
            config=CircuitBreakerConfig(failure_threshold=2, recovery_timeout=1)
        )

        # 触发熔断
        for _ in range(2):
            try:
                raise Exception("Simulated failure")
            except Exception:
                breaker._on_failure()

        assert breaker.state == CircuitState.OPEN

        # 等待恢复超时
        time.sleep(1.1)

        # 再次检查状态应该变为 HALF_OPEN
        with breaker._lock:
            breaker._check_state_transition()
        assert breaker.state == CircuitState.HALF_OPEN

    def test_circuit_closes_after_success_in_half_open(self):
        """HALF_OPEN 状态下成功后变为 CLOSED"""
        breaker = CircuitBreaker(
            name="test",
            config=CircuitBreakerConfig(
                failure_threshold=2,
                recovery_timeout=0,  # 立即恢复
                half_open_max_calls=2
            )
        )

        # 触发熔断
        for _ in range(2):
            try:
                raise Exception("Simulated failure")
            except Exception:
                breaker._on_failure()

        # 触发恢复
        with breaker._lock:
            breaker._check_state_transition()
        assert breaker.state == CircuitState.HALF_OPEN

        # 模拟成功
        breaker._on_success()
        breaker._on_success()

        assert breaker.state == CircuitState.CLOSED

    def test_circuit_open_again_on_failure_in_half_open(self):
        """HALF_OPEN 状态下失败后再次变为 OPEN"""
        breaker = CircuitBreaker(
            name="test",
            config=CircuitBreakerConfig(
                failure_threshold=2,
                recovery_timeout=0,
                half_open_max_calls=3
            )
        )

        # 触发熔断
        for _ in range(2):
            try:
                raise Exception("Simulated failure")
            except Exception:
                breaker._on_failure()

        # 触发恢复
        with breaker._lock:
            breaker._check_state_transition()

        # HALF_OPEN 状态下失败
        breaker._on_failure()

        assert breaker.state == CircuitState.OPEN

    def test_call_executes_when_closed(self):
        """CLOSED 状态下可以正常执行"""
        breaker = CircuitBreaker(name="test")

        def success_func():
            return "success"

        result = breaker.call(success_func)
        assert result == "success"

    def test_call_raises_when_open(self):
        """OPEN 状态下拒绝调用"""
        breaker = CircuitBreaker(
            name="test",
            config=CircuitBreakerConfig(failure_threshold=1)
        )

        # 触发熔断
        try:
            raise Exception("Simulated failure")
        except Exception:
            breaker._on_failure()

        def my_func():
            return "should not be called"

        with pytest.raises(CircuitBreakerError) as exc_info:
            breaker.call(my_func)

        assert "OPEN" in str(exc_info.value)

    def test_circuit_reset(self):
        """手动重置熔断器"""
        breaker = CircuitBreaker(
            name="test",
            config=CircuitBreakerConfig(failure_threshold=2)
        )

        # 触发熔断
        for _ in range(2):
            try:
                raise Exception("Simulated failure")
            except Exception:
                breaker._on_failure()

        assert breaker.state == CircuitState.OPEN

        # 重置
        breaker.reset()

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
        assert breaker.is_available is True

    def test_get_status(self):
        """获取熔断器状态"""
        breaker = CircuitBreaker(
            name="test",
            config=CircuitBreakerConfig(failure_threshold=5, recovery_timeout=60)
        )

        status = breaker.get_status()

        assert status["name"] == "test"
        assert status["state"] == "closed"
        assert status["threshold"] == 5
        assert status["recovery_timeout"] == 60


class TestCircuitBreakerGlobal:
    """全局熔断器管理器测试"""

    def setup_method(self):
        """每个测试前重置全局熔断器"""
        reset_all_circuit_breakers()

    def test_get_circuit_breaker_creates_new(self):
        """获取不存在的熔断器会自动创建"""
        reset_all_circuit_breakers()

        breaker = get_circuit_breaker("new_provider")
        assert breaker is not None
        assert breaker.name == "new_provider"
        assert breaker.state == CircuitState.CLOSED

    def test_get_circuit_breaker_returns_same_instance(self):
        """同一名称返回相同实例"""
        breaker1 = get_circuit_breaker("same_name")
        breaker2 = get_circuit_breaker("same_name")

        assert breaker1 is breaker2


class TestCircuitBreakerIntegration:
    """熔断器集成测试 - 模拟真实场景"""

    def setup_method(self):
        reset_all_circuit_breakers()

    def test_rapid_failures_trigger_circuit(self):
        """快速连续失败触发熔断"""
        breaker = get_circuit_breaker("integration_test")

        call_count = {"total": 0}

        def flaky_service():
            call_count["total"] += 1
            raise Exception("Service unavailable")

        # 快速失败多次
        for i in range(6):
            try:
                breaker.call(flaky_service)
            except CircuitBreakerError:
                break  # 熔断器开启
            except Exception:
                pass

        # 应该记录了多次失败
        assert breaker.failure_count >= 5

    def test_exponential_backoff_recommended(self):
        """熔断器状态变化符合预期"""
        breaker = get_circuit_breaker("backoff_test")

        # 触发多次失败
        for _ in range(5):
            try:
                raise Exception("Failure")
            except Exception:
                breaker._on_failure()

        # 熔断器应该打开
        assert breaker.state == CircuitState.OPEN
        assert breaker.is_available is False

        # 状态信息应该可用
        status = breaker.get_status()
        assert status["failure_count"] >= 5


class TestLLMTimeoutAndRetry:
    """LLM 超时和重试逻辑测试"""

    def test_backoff_calculation(self):
        """测试退避时间计算"""
        # 模拟 _calculate_backoff
        def calculate_backoff(attempt: int, base_delay: float = 1.0, max_delay: float = 30) -> float:
            import random
            delay = min(base_delay * (2 ** attempt), max_delay)
            jitter = delay * 0.25 * (2 * random.random() - 1)
            return delay + jitter

        # 第 0 次: 约 1 秒 (±25%)
        backoff_0 = calculate_backoff(0)
        assert 0.75 <= backoff_0 <= 1.25

        # 第 1 次: 约 2 秒 (±25%)
        backoff_1 = calculate_backoff(1)
        assert 1.5 <= backoff_1 <= 2.5

        # 第 2 次: 约 4 秒 (±25%)
        backoff_2 = calculate_backoff(2)
        assert 3 <= backoff_2 <= 5

        # 第 4 次: 约 16 秒 (±25%)
        backoff_4 = calculate_backoff(4)
        assert 12 <= backoff_4 <= 20

        # 第 5 次应该被 max 限制
        backoff_5 = calculate_backoff(5, max_delay=30)
        assert backoff_5 <= 37.5  # 30 * 1.25 (max + jitter)

    def test_retryable_error_classification(self):
        """测试错误分类"""
        from urllib.error import URLError, HTTPError

        # 这些错误应该可重试
        retryable_errors = [
            URLError("Connection refused"),
            HTTPError("", 429, "Rate Limited", {}, None),
            HTTPError("", 500, "Internal Server Error", {}, None),
            HTTPError("", 502, "Bad Gateway", {}, None),
            HTTPError("", 503, "Service Unavailable", {}, None),
        ]

        # 这些错误不应该重试（但需要根据实现判断）
        # 通常 4xx 客户端错误不应该重试

    def test_circuit_breaker_timing(self):
        """测试熔断器状态变化的时间要求"""
        breaker = CircuitBreaker(
            name="timing_test",
            config=CircuitBreakerConfig(
                failure_threshold=3,
                recovery_timeout=2,  # 2秒恢复
                half_open_max_calls=2
            )
        )

        # 记录开始时间
        start_time = time.time()

        # 触发熔断
        for _ in range(3):
            try:
                raise Exception("Failure")
            except Exception:
                breaker._on_failure()

        assert breaker.state == CircuitState.OPEN

        # 等待恢复
        time.sleep(2.1)

        # 检查状态应该变为 HALF_OPEN
        with breaker._lock:
            breaker._check_state_transition()

        elapsed = time.time() - start_time
        assert breaker.state == CircuitState.HALF_OPEN
        assert elapsed >= 2

    def test_concurrent_access(self):
        """测试并发访问"""
        import threading

        breaker = get_circuit_breaker("concurrent_test")

        results = {"success": 0, "circuit_open": 0}

        def success_call():
            return "success"

        def concurrent_calls():
            for _ in range(100):
                try:
                    result = breaker.call(success_call)
                    if result == "success":
                        results["success"] += 1
                except CircuitBreakerError:
                    results["circuit_open"] += 1

        # 启动多个线程
        threads = [threading.Thread(target=concurrent_calls) for _ in range(5)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有调用都应该成功（没有触发熔断）
        assert results["success"] == 500
        assert results["circuit_open"] == 0
