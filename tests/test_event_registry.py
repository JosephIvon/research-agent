"""SSE 事件名登记表（TaskEventType）契约测试。

为什么需要它：这个枚举此前是装饰性的——工作流全程用裸字符串发事件，从不查它。
`citation_check` / `injection_detected` / `partial` 三个真实事件压根不在册，
而打错一个名字也没人会发现：前端只会安静地少收一个事件，没有任何报错能指认。

现在 `_emit` 拿事件名对表。这组测试钉住三件事：
1. 工作流实际发出的事件都在册（真跑一遍，不做源码文本匹配）
2. 未登记的名字会被当场拒绝，而不是悄悄发出去
3. partial 走枚举而不是裸字符串
"""
import asyncio

import pytest

from src.llm import ScriptedLLMClient
from src.workflow.mvp_workflow import MVPWorkflow
from src.workflow.task_store import KNOWN_EVENT_NAMES, TaskEventType


@pytest.fixture
def offline_workflow(monkeypatch):
    """构造 MVPWorkflow 不碰真实密钥：LLM 客户端换成脚本件。

    构造器会去 get_llm_client()，那是真客户端——直接 new 会因为缺
    MINIMAX_API_KEY 直接炸，测的就不是事件名而是环境变量了。
    """
    from src.workflow import mvp_workflow

    monkeypatch.setattr(
        mvp_workflow, "get_llm_client",
        lambda *a, **k: ScriptedLLMClient(script={"react": ["占位，本用例不会真的调用模型"]}),
    )
    return MVPWorkflow


class _Collector:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)

    def names(self):
        return [e.event for e in self.events]


def test_partial_is_a_registered_event_type():
    assert TaskEventType.PARTIAL.value == "partial"
    assert "partial" in KNOWN_EVENT_NAMES


def test_every_real_event_name_is_registered():
    """这三个此前根本不在册——枚举看着齐全，其实漏了工作流真在发的三个。"""
    for value in ("citation_check", "injection_detected", "partial"):
        assert value in KNOWN_EVENT_NAMES, f"{value} 是工作流真会发的事件，必须在册"


def test_emit_rejects_an_unregistered_event_name(offline_workflow):
    """打错一个字母就必须炸。安静地少发一个事件，比报错难查得多。"""
    workflow = offline_workflow()
    with pytest.raises(ValueError) as exc:
        workflow._emit("crawl_progres", "crawl", "running", "打错一个字母")
    assert "crawl_progres" in str(exc.value)
    assert "TaskEventType" in str(exc.value)


def test_emit_rejects_unknown_name_even_without_subscriber(offline_workflow):
    """没有 emitter 时也必须对表——否则测试环境里打错的名字会被完全掩盖。"""
    workflow = offline_workflow()  # 未挂 event_emitter
    with pytest.raises(ValueError):
        workflow._emit("parital", "completed", "partial", "打错一个字母")


def test_emit_accepts_every_registered_name(offline_workflow):
    """反方向也要钉：登记表里的名字不该被误伤。"""
    workflow = offline_workflow()
    for name in sorted(KNOWN_EVENT_NAMES):
        workflow._emit(name, "test", "running", f"{name} 应当被接受")


def test_partial_event_goes_out_under_the_registered_name(tmp_path, monkeypatch):
    """真实 runner 跑出的 partial 事件名，必须与枚举一致。"""
    import json

    from src.agent import config
    from src.workflow import mvp_workflow
    from tests.test_partial_status import (
        _call,
        _drive,
        _react_llm,
        _wire_offline,
        _wire_react_tools,
    )

    monkeypatch.setenv("AGENT_MODE", "react")
    llm = _react_llm(_call("web_search", query="q"), "## 部分结论\n\n预算耗尽前采集。")
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch)
    monkeypatch.setattr(config, "AGENT_TOKEN_BUDGET", 10)
    monkeypatch.setattr(mvp_workflow, "verify_report", lambda report, store: {
        "passed": True, "coverage": 1.0, "valid_ratio": 1.0, "grounding": 1.0,
        "violations": [],
    })

    collector = _Collector()
    workflow = MVPWorkflow(event_emitter=collector)
    asyncio.run(_drive(workflow))

    emitted = collector.names()
    assert emitted, "本用例需要真的跑到事件流，空事件说明接线坏了"
    assert TaskEventType.PARTIAL.value in emitted
    unregistered = [n for n in emitted if n not in KNOWN_EVENT_NAMES]
    assert not unregistered, f"发出了未登记的事件名：{unregistered}"
