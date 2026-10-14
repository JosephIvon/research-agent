"""可观测层测试：contextvars 隔离（线程池安全）与 no-op 降级。

红线（实施草案 §6-1）：task_id 关联禁止实例属性——LLM 调用进线程池后
并发任务会在共享实例上互相覆盖。【模块】草案的 `self._current_task_id`
写法正是这个坑；本文件锁定 contextvars 行为。
"""
import asyncio

from src.observability import (
    clear_task_context,
    current_task_id,
    is_enabled,
    llm_span,
    set_task_context,
)
from src.observability import langfuse_tracer


def test_disabled_by_default_is_zero_overhead():
    """LANGFUSE_ENABLED 未开时一切 API 是 no-op，不抛异常。"""
    assert is_enabled() is False
    assert current_task_id() is None
    with llm_span("llm.call", model="m") as span:
        assert span is None
    assert langfuse_tracer.start_trace("research") is None
    langfuse_tracer.end_trace({"ok": True})  # 不抛
    assert langfuse_tracer.trace_url("task-x") is None


def test_task_context_roundtrip():
    set_task_context("task-42", user_id="u1")
    assert current_task_id() == "task-42"
    clear_task_context()
    assert current_task_id() is None


def test_contextvars_survive_to_thread_and_isolate_concurrent_tasks():
    """并发任务各自 to_thread 执行时，线程内看到的必须是各自的 task_id。

    这是【模块】草案实例属性写法会串号的精确场景：两个协程交替把
    task_id 写到"共享实例"上，线程池里的读数互相污染。
    """
    observed = {}

    async def task_worker(task_id: str):
        set_task_context(task_id)
        # 模拟 achat_completion：提交到线程池，快照随任务带走
        def llm_call_in_thread():
            return current_task_id()
        observed[task_id] = await asyncio.to_thread(llm_call_in_thread)
        clear_task_context()

    async def main():
        await asyncio.gather(task_worker("task-a"), task_worker("task-b"))

    asyncio.run(main())
    assert observed == {"task-a": "task-a", "task-b": "task-b"}


def test_child_context_does_not_leak_to_sibling():
    """一个协程设置的上下文不得泄给兄弟协程（contextvars 快照语义）。"""
    results = []

    async def child(tag: str):
        if tag == "setter":
            set_task_context("task-setter")
            await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(0.02)
            results.append((tag, current_task_id()))

    async def main():
        await asyncio.gather(child("setter"), child("reader"))

    asyncio.run(main())
    tag, tid = results[0]
    assert tag == "reader" and tid is None, "setter 的上下文泄漏给了兄弟协程"
