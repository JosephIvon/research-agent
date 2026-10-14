"""TaskBudget / BudgetGuard（M6 token 与成本计量）单元测试。"""
import threading

from src.llm.token_budget import (
    BudgetExceeded,
    BudgetGuard,
    TaskBudget,
    count_tokens,
    price_for,
)


def test_count_tokens_is_positive_and_monotonic():
    assert count_tokens("") == 0
    assert 0 < count_tokens("hello world") <= 8
    assert count_tokens("你好世界" * 100) > count_tokens("你好世界")


def test_price_for_matches_model_substring():
    assert price_for("deepseek-chat")["input"] == 0.14
    assert price_for("SomeGLM-4-Plus")["input"] == 0.10
    default = price_for("totally-unknown-model")
    assert default == price_for(None)


def test_record_accumulates_usage_by_model():
    budget = TaskBudget("t1", max_tokens=1000, max_usd=1.0)
    budget.record("minimax", "abab6.5s", prompt_tokens=100, completion_tokens=50)
    budget.record("minimax", "abab6.5s", prompt_tokens=200, completion_tokens=150)
    budget.record("deepseek", "deepseek-chat", prompt_tokens=1000, completion_tokens=0)

    snap = budget.snapshot()
    assert snap["prompt_tokens"] == 1300 and snap["completion_tokens"] == 200
    assert snap["total_tokens"] == 1500 and snap["calls"] == 3
    assert snap["estimated"] is False
    # deepseek 1000 input ≈ 0.14/1M*1000 = 0.00014；abab 300/200 ≈ 0.00005
    assert 0.0001 < snap["estimated_usd"] < 0.001


def test_exceeded_reason_covers_token_and_usd():
    token_tight = TaskBudget("t", max_tokens=10, max_usd=10)
    token_tight.record("x", "m", 100, 0)
    assert token_tight.exceeded_reason().startswith("token_budget_exceeded")

    usd_tight = TaskBudget("t", max_tokens=10**9, max_usd=0.0)
    usd_tight.record("x", "gpt-4o", 1_000_000, 0)  # 1M input ≈ $2.5
    assert usd_tight.exceeded_reason().startswith("usd_budget_exceeded")

    fine = TaskBudget("t", max_tokens=100, max_usd=1.0)
    fine.record("x", "m", 10, 10)
    assert fine.exceeded_reason() is None


def test_estimated_records_are_flagged():
    budget = TaskBudget("t")
    budget.record_estimated("fake", "scripted", "x" * 400, "y" * 40)
    snap = budget.snapshot()
    assert snap["estimated"] is True, "估算记账必须打标，不得冒充精确值"
    assert snap["prompt_tokens"] > 0


def test_budget_guard_lifecycle():
    guard = BudgetGuard()
    guard.create("task-a", max_tokens=50, max_usd=1)
    budget = guard.get("task-a")
    assert budget is not None and budget.max_tokens == 50

    report = guard.release("task-a")
    assert report["task_id"] == "task-a"
    assert guard.get("task-a") is None
    assert guard.release("task-a") is None


def test_budget_is_thread_safe():
    """LLM 记账发生在 to_thread 线程池——并发记账不得丢次数。"""
    budget = TaskBudget("t", max_tokens=10**9, max_usd=10)
    threads = [
        threading.Thread(target=lambda: budget.record("p", "m", 100, 100))
        for _ in range(50)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert budget.snapshot()["calls"] == 50
    assert budget.snapshot()["total_tokens"] == 50 * 200


def test_budget_exception_carries_report():
    budget = TaskBudget("t", max_tokens=1, max_usd=1)
    budget.record("p", "m", 999, 0)
    reason = budget.exceeded_reason()
    exc = BudgetExceeded(reason, budget.snapshot())
    assert "token_budget_exceeded" in exc.reason
    assert exc.report["total_tokens"] == 999
