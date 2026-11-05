"""契约 v2（2026-11-04）验收测试：工具元数据 / 错误码 / 输出游标 / RunOutcome。

设计目标不是“函数能跑”，而是证明断言有牙：
- 声明只读却实际写的工具必须在注册时被拒绝；
- 未登记的错误码必须在注册时被拒绝；
- 分页参数不会漏给 handler，未传参时返回与改造前一致；
- Runner 的 stop_reason 必须能从 ReasonCode 反查，不存在裸字符串。
"""
import asyncio

import pytest

from src.agent.error_codes import ReasonCode, RunOutcome, ToolErrorCode
from src.agent.runner import ReActRunner
from src.agent.tool_specs import build_default_tools
from src.agent.trace import STOP_REASONS
from src.agent.tools import Tool, ToolRegistry
from src.llm import ScriptedLLMClient


def _registry() -> ToolRegistry:
    return ToolRegistry()


def _tool(**kwargs) -> Tool:
    return Tool(
        name=kwargs.pop("name", "demo"),
        description="demo",
        parameters=kwargs.pop(
            "parameters",
            {"type": "object", "properties": {}, "required": []},
        ),
        handler=kwargs.pop("handler", lambda: None),
        **kwargs,
    )


# --------------------------------------------------------------------------
# A1：契约元数据与注册校验
# --------------------------------------------------------------------------

def test_read_only_tool_declared_as_write_is_rejected():
    """牙齿测试：read_only=True + side_effects='write' 必须注册失败。"""
    registry = _registry()
    with pytest.raises(ValueError, match="side_effects='write'"):
        registry.add(_tool(name="sneaky_writer", side_effects="write"))


def test_unknown_side_effects_value_is_rejected():
    registry = _registry()
    with pytest.raises(ValueError, match="side_effects"):
        registry.add(_tool(name="bad_side_effect", side_effects="network"))


def test_unknown_error_code_is_rejected():
    registry = _registry()
    with pytest.raises(ValueError, match="未登记的错误码"):
        registry.add(_tool(name="bad_error_code", error_codes=("nope",)))


def test_non_read_only_tool_still_rejected():
    registry = _registry()
    with pytest.raises(ValueError, match="只读"):
        registry.add(_tool(name="writer", read_only=False))


def test_tool_metadata_is_declared_for_all_default_tools():
    registry = build_default_tools(None, {})
    assert registry.names(), "默认工具集不应为空"
    for name in registry.names():
        tool = registry.get(name)
        assert tool.read_only is True
        assert tool.side_effects == "read"
        assert isinstance(tool.idempotent, bool)
        assert tool.error_codes, f"{name} 必须显式声明非空 error_codes"
        assert set(tool.error_codes) <= set(code.value for code in ToolErrorCode)


def test_generate_research_report_declares_contract_metadata():
    """编排工具也要声明契约元数据；付费流水线不幂等是给重试决策的信号。"""
    from src.agent.tool_specs import generate_research_report_tool

    tool = generate_research_report_tool(lambda: None, 300.0)
    assert tool.side_effects == "read"
    assert tool.idempotent is False
    assert "timeout" in tool.error_codes


# --------------------------------------------------------------------------
# A1：execute 统一返回新增 error_code / retryable
# --------------------------------------------------------------------------

def test_unknown_tool_returns_structured_error_code():
    result = asyncio.run(_registry().execute("missing", {}))
    assert result["ok"] is False
    assert result["error_code"] == ToolErrorCode.UNKNOWN_TOOL.value
    assert result["retryable"] is False


def test_invalid_arguments_returns_structured_error_code():
    registry = _registry()
    registry.add(_tool(
        name="needs_query",
        parameters={"type": "object", "properties": {"query": {"type": "string"}},
                    "required": ["query"]},
    ))
    result = asyncio.run(registry.execute("needs_query", {}))
    assert result["ok"] is False
    assert result["error_code"] == ToolErrorCode.INVALID_ARGUMENTS.value
    assert result["retryable"] is False


def test_timeout_and_execution_error_are_retryable():
    async def _slow():
        await asyncio.sleep(0.05)
        return "late"

    registry = _registry()
    registry.add(_tool(name="slow", handler=_slow, timeout=0.01))
    result = asyncio.run(registry.execute("slow", {}))
    assert result["error_code"] == ToolErrorCode.TIMEOUT.value
    assert result["retryable"] is True

    def _boom():
        raise RuntimeError("boom")

    registry.add(_tool(name="boom", handler=_boom))
    result = asyncio.run(registry.execute("boom", {}))
    assert result["error_code"] == ToolErrorCode.EXECUTION_ERROR.value
    assert result["retryable"] is True


def test_unsafe_url_returns_unsafe_url_code():
    def _reject(url):
        raise ValueError("blocked")

    registry = ToolRegistry(url_validator=_reject)
    registry.add(_tool(name="fetcher", url_params=("url",)))
    result = asyncio.run(registry.execute("fetcher", {"url": "http://evil.test"}))
    assert result["error_code"] == ToolErrorCode.UNSAFE_URL.value
    assert result["retryable"] is False


# --------------------------------------------------------------------------
# A1.5：ToolContractError —— handler 声明错误码，注册表按码归一
# --------------------------------------------------------------------------

def test_contract_error_maps_to_declared_code_and_is_not_retryable():
    from src.agent.error_codes import ToolContractError

    def _unmet():
        raise ToolContractError(ToolErrorCode.PRECONDITION_FAILED, "先做前置步骤")

    registry = _registry()
    registry.add(_tool(name="needs_prep", handler=_unmet))
    result = asyncio.run(registry.execute("needs_prep", {}))
    assert result["ok"] is False
    assert result["error_code"] == ToolErrorCode.PRECONDITION_FAILED.value
    assert result["retryable"] is False
    assert result["error"] == "先做前置步骤"


def test_extract_facts_without_fetch_is_precondition_not_execution_error():
    """真实工具验收：未先 fetch_site 就抽取，必须是 precondition_failed。

    语义牙：此前它抛裸 ValueError 被兜底成 execution_error（retryable=True），
    模型会被误导去原样重试一个必然再失败的调用。
    """
    registry = build_default_tools(None, {})
    result = asyncio.run(registry.execute(
        "extract_facts", {"url": "https://never-fetched.test/"}
    ))
    assert result["ok"] is False
    assert result["error_code"] == ToolErrorCode.PRECONDITION_FAILED.value
    assert result["retryable"] is False
    assert "尚未抓取" in result["error"]


def test_read_evidence_unknown_key_is_not_found():
    registry = build_default_tools(None, {})
    result = asyncio.run(registry.execute("read_evidence", {"key": "ev_deadbeef00000000"}))
    assert result["ok"] is False
    assert result["error_code"] == ToolErrorCode.NOT_FOUND.value
    assert result["retryable"] is False


# --------------------------------------------------------------------------
# A2：输出游标
# --------------------------------------------------------------------------

def test_pagination_slices_list_and_sets_next_offset():
    async def _items():
        return ["a", "b", "c", "d", "e"]

    registry = _registry()
    registry.add(_tool(name="listy", handler=_items, paginated=True))
    result = asyncio.run(registry.execute("listy", {"offset": 1, "limit": 2}))
    assert result["ok"] is True
    assert result["data"] == ["b", "c"]
    assert result["nextOffset"] == 3


def test_pagination_without_args_keeps_legacy_shape():
    async def _items():
        return ["a", "b"]

    registry = _registry()
    registry.add(_tool(name="listy", handler=_items, paginated=True))
    result = asyncio.run(registry.execute("listy", {}))
    assert result["data"] == ["a", "b"]
    assert result["nextOffset"] is None


def test_pagination_args_are_not_passed_to_handler():
    seen = {}

    async def _items(**kwargs):
        seen.update(kwargs)
        return ["a", "b", "c"]

    registry = _registry()
    registry.add(_tool(name="listy", handler=_items, paginated=True))
    asyncio.run(registry.execute("listy", {"offset": 0, "limit": 2, "extra": "x"}))
    assert "offset" not in seen and "limit" not in seen


def test_pagination_on_dict_field_does_not_mutate_cache():
    cached = {"competitors": ["a", "b", "c"], "note": "cached"}

    async def _payload():
        return cached

    registry = _registry()
    registry.add(_tool(
        name="cached_tool", handler=_payload, paginated=True, page_field="competitors"
    ))
    result = asyncio.run(registry.execute("cached_tool", {"offset": 1, "limit": 1}))
    assert result["data"]["competitors"] == ["b"]
    assert result["data"]["note"] == "cached"
    assert result["nextOffset"] == 2
    # 原缓存对象不得被切片污染
    assert cached["competitors"] == ["a", "b", "c"]


def test_pagination_schema_is_injected_without_touching_declared_properties():
    registry = build_default_tools(None, {})
    schema = registry.get("web_search").to_schema()
    props = schema["function"]["parameters"]["properties"]
    assert "offset" in props and "limit" in props
    # 原始声明不被改写（deepcopy）
    assert "offset" not in registry.get("web_search").parameters["properties"]


# --------------------------------------------------------------------------
# A3：RunOutcome / reason_code
# --------------------------------------------------------------------------

def test_reason_code_covers_legacy_stop_reasons():
    legacy = {"halt", "max_steps", "budget_exceeded", "error",
              "empty_observations", "duplicate_loop"}
    assert set(STOP_REASONS) == legacy
    assert ReasonCode.CANCELLED.value == "cancelled"


def test_runner_returns_reason_code_and_outcome():
    llm = ScriptedLLMClient(script={"react": ["## 结论\n\n完成。"]})
    result = asyncio.run(ReActRunner(llm, max_steps=3).run("调研"))
    assert result.stop_reason == "halt"
    assert result.reason_code is ReasonCode.HALT
    assert result.outcome == RunOutcome(ReasonCode.HALT)
    assert result.outcome.to_dict() == {
        "reason_code": "halt",
        "message": "",
        "unread_folded_evidence": 0,
        "unread_cited_evidence": 0,
    }


def test_run_outcome_roundtrip():
    outcome = RunOutcome(ReasonCode.BUDGET_EXCEEDED, "token 用尽")
    restored = RunOutcome.from_dict(outcome.to_dict())
    assert restored == outcome
