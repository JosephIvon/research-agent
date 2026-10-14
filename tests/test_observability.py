"""可观测层测试：contextvars 隔离（线程池安全）、no-op 降级与**启用态**可用性。

红线（实施草案 §6-1）：task_id 关联禁止实例属性——LLM 调用进线程池后
并发任务会在共享实例上互相覆盖。【模块】草案的 `self._current_task_id`
写法正是这个坑；本文件锁定 contextvars 行为。

启用态测试用内存桩替换 SDK client（monkeypatch 掉 `sys.modules["langfuse"]`），
**不连 Langfuse、不需要 API key、不产生网络调用**：只验证 `_init()` 之后的
trace/span 生命周期真的能跑通，而不是永远在 no-op 分支里返回 None。
"""
import asyncio
import sys
import types

import pytest

from src.observability import (
    clear_task_context,
    current_task_id,
    is_enabled,
    llm_span,
    set_task_context,
)
from src.observability import langfuse_tracer


def test_disabled_by_default_is_zero_overhead(monkeypatch):
    """LANGFUSE_ENABLED 未开时一切 API 是 no-op，不抛异常。"""
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)  # 开发者本机开了也不能影响本断言
    assert is_enabled() is False
    assert current_task_id() is None
    with llm_span("llm.call", model="m") as span:
        assert span is None
    assert langfuse_tracer.start_trace("research") is None
    langfuse_tracer.end_trace({"ok": True})  # 不抛
    assert langfuse_tracer.trace_url("task-x") is None
    assert langfuse_tracer.current_trace() is None


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


# --------------------------------------------------------------------------
# 启用态：SDK 换成内存桩，验证 trace/span 生命周期真的能跑通
# --------------------------------------------------------------------------

class _FakeGeneration:
    """Langfuse generation span 的最小替身。"""

    def __init__(self, name, model, model_parameters):
        self.name = name
        self.model = model
        self.model_parameters = model_parameters
        self.ended = []

    def end(self, **kwargs):
        self.ended.append(kwargs)


class _FakeTrace:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.generations = []
        self.updates = []

    def generation(self, name, model=None, model_parameters=None):
        span = _FakeGeneration(name, model, model_parameters)
        self.generations.append(span)
        return span

    def update(self, output=None):
        self.updates.append(output)


class _FakeLangfuse:
    instances = []

    def __init__(self, public_key=None, secret_key=None, host=None):
        self.public_key = public_key
        self.secret_key = secret_key
        self.host = host
        self.traces = []
        _FakeLangfuse.instances.append(self)

    def trace(self, id, name, user_id=None, input=None):
        trace = _FakeTrace(id=id, name=name, user_id=user_id, input=input)
        self.traces.append(trace)
        return trace


@pytest.fixture
def enabled(monkeypatch):
    """打开 LANGFUSE_ENABLED 并塞入桩 SDK，返回桩 client 实例。

    teardown 把模块级 `_ENABLED`/`_langfuse` 与 contextvars 全部还原——
    否则启用态会污染后面的 no-op 测试（那正是要守住的零开销契约）。
    """
    module = types.ModuleType("langfuse")
    module.Langfuse = _FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", module)
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lc-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lc-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.test")
    monkeypatch.setattr(langfuse_tracer, "_langfuse", None)
    monkeypatch.setattr(langfuse_tracer, "_ENABLED", False)

    _FakeLangfuse.instances.clear()
    langfuse_tracer.clear_task_context()
    langfuse_tracer._init()

    yield _FakeLangfuse.instances[-1]

    langfuse_tracer.clear_task_context()


def test_enabled_state_is_a_real_client_not_a_noop(enabled):
    assert is_enabled() is True
    assert enabled.public_key == "pk-lc-test" and enabled.host == "https://langfuse.test"


def test_start_trace_returns_trace_and_latches_it_in_context(enabled):
    set_task_context("task-42", user_id="u1")

    trace = langfuse_tracer.start_trace("research", input={"q": "acme"})

    assert trace is not None, "启用态下 start_trace 必须返回真实 trace，不能是 None"
    assert langfuse_tracer.current_trace() is trace
    assert enabled.traces == [trace]
    assert trace.kwargs == {
        "id": "task-42", "name": "research", "user_id": "u1", "input": {"q": "acme"}}


def test_llm_span_attaches_to_current_trace_and_ends(enabled):
    set_task_context("task-42")
    trace = langfuse_tracer.start_trace("research")

    with llm_span("llm.step1", model="stub-model", model_parameters={"temperature": 0.2}) as span:
        assert span is not None, "启用态下 span 不能是 None，否则 trace 里没有 LLM 记录"
        assert trace.generations == [span]
        assert span.model == "stub-model" and span.name == "llm.step1"

    assert span.ended == [{}], "正常结束必须调用 end()"


def test_end_trace_writes_output_then_clears_context(enabled):
    set_task_context("task-42")
    trace = langfuse_tracer.start_trace("research")

    langfuse_tracer.end_trace({"answer": "ok"})

    assert trace.updates == [{"answer": "ok"}]
    assert langfuse_tracer.current_trace() is None, "end_trace 必须清掉 trace，避免串到下一个任务"


def test_trace_url_is_available_only_while_enabled(enabled):
    assert langfuse_tracer.trace_url("task-42") == "https://langfuse.test/trace/task-42"
    set_task_context("task-43")
    assert langfuse_tracer.trace_url() == "https://langfuse.test/trace/task-43"


def test_start_trace_without_task_id_does_not_create_a_broken_trace(enabled):
    """trace id 直接用 task_id：拿不到 task_id 就不能开 trace，否则控制台里是
    一堆 id 为空的孤儿 trace，且 trace_url 会拼出 /trace/None。"""
    assert langfuse_tracer.start_trace("research") is None
    assert enabled.traces == []
    assert langfuse_tracer.current_trace() is None


def test_llm_span_without_trace_stays_silent_when_enabled(enabled):
    set_task_context("task-42")  # 有 task_id 但没开 trace
    with llm_span("llm.step1", model="stub-model") as span:
        assert span is None
    assert enabled.traces == []


def test_llm_span_marks_error_level_and_reraises(enabled):
    set_task_context("task-42")
    trace = langfuse_tracer.start_trace("research")

    with pytest.raises(RuntimeError, match="工具炸了"):
        with llm_span("llm.step1", model="stub-model"):
            raise RuntimeError("工具炸了")

    assert trace.generations[0].ended == [
        {"level": "ERROR", "status_message": "工具炸了"}]


def test_concurrent_tasks_get_their_own_trace_under_enabled_state(enabled):
    """启用态下并发也不串号：to_thread 里看到的 trace 必须属于本任务。"""
    async def task_worker(tag):
        set_task_context(tag)
        langfuse_tracer.start_trace(f"research-{tag}")

        def _in_thread():
            return langfuse_tracer.current_trace().kwargs["id"]

        seen = await asyncio.to_thread(_in_thread)
        clear_task_context()
        return seen

    async def main():
        return await asyncio.gather(task_worker("task-a"), task_worker("task-b"))

    assert asyncio.run(main()) == ["task-a", "task-b"]


def test_init_failure_degrades_to_noop_without_raising(monkeypatch):
    """启用态最危险的分支：SDK 不可用时必须整体降级为 no-op，绝不能把
    主流程带崩（密钥写错、包版本不兼容都会走到这里）。"""
    monkeypatch.setitem(sys.modules, "langfuse", None)  # import 时直接失败
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setattr(langfuse_tracer, "_langfuse", None)
    monkeypatch.setattr(langfuse_tracer, "_ENABLED", False)

    langfuse_tracer._init()

    assert is_enabled() is False
    set_task_context("task-42")
    with llm_span("llm.step1", model="m") as span:
        assert span is None
    assert langfuse_tracer.start_trace("research") is None
    langfuse_tracer.end_trace({"ok": True})
    assert langfuse_tracer.trace_url("task-42") is None
    clear_task_context()


def test_disabled_env_keeps_everything_zero_overhead_even_with_a_loadable_sdk(monkeypatch):
    """开关没开就不许初始化——哪怕 SDK 装好了、能成功构造 client。"""
    module = types.ModuleType("langfuse")
    module.Langfuse = _FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", module)
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    monkeypatch.setattr(langfuse_tracer, "_langfuse", None)
    monkeypatch.setattr(langfuse_tracer, "_ENABLED", False)

    _FakeLangfuse.instances.clear()
    langfuse_tracer._init()

    assert is_enabled() is False
    assert _FakeLangfuse.instances == [], "未启用时不得构造 client（零开销硬要求）"
