"""M6 PARTIAL 终态契约：预算耗尽收束的落库口径、SSE 事件与 usage 记账。

零网络零密钥：LLM 走 ScriptedLLMClient，partial 分支用桩 Runner 直接吐出
预算耗尽的 AgentResult，交付链（引用校验 → 报告落盘 → SQLite）保持真实执行。
锚点语义：

- 预算耗尽**且已产出终答** → status=partial、发 partial 事件、**不发** completed
- 预算耗尽**且无终答** → 仍按 failed 收束（partial 只描述"有结论但被截断"）
- 正常 halt → 仍是 completed 事件（partial 分支不得外溢）
- AgentStep.stop_reason_if_any 必须真有值，agent_step 事件 payload 拿得到
- AgentResult.usage_estimated 真实反映"本轮是否只拿到估算值"
"""
import asyncio
import json
from typing import Any, Dict, List, get_args

from src.agent import tool_specs
from src.agent.runner import ReActRunner
from src.agent.trace import AgentResult, AgentStep
from src.llm import ScriptedLLMClient
from src.schema.state import ResearchState
from src.workflow.task_store import TaskStatus

SEARCH_HIT = [{"title": "Acme 官网", "url": "https://acme.example.com", "content": "AI 调研工具"}]

PARTIAL_ANSWER = "## 结论\n\n预算耗尽前已确认的部分结论。"


class _FakeSearchTool:
    def __init__(self, results):
        self.results = results

    def search(self, query, num_results=None, provider=None):
        return self.results


class _EventCollector:
    """事件桩：挂在 workflow._event_emitter 上收集 emit 的事件对象。"""

    def __init__(self):
        self.events: List[Any] = []

    async def emit(self, event):
        self.events.append(event)

    def of(self, event_name: str) -> List[Any]:
        return [e for e in self.events if e.event == event_name]


def _call(name, **args):
    return {"type": "tool_call", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def _react_llm(*entries):
    return ScriptedLLMClient(script={"react": list(entries)})


class _UsageLLM(ScriptedLLMClient):
    """真值 usage 供给者：在 raw 返回里补 usage（做法照抄 test_react_runner）。"""

    def chat_completion_raw(self, *a, **k):
        raw = super().chat_completion_raw(*a, **k)
        raw["usage"] = {"prompt_tokens": 120, "completion_tokens": 30}
        return raw


class _MixedUsageLLM(ScriptedLLMClient):
    """混合供给者：只有第二轮才给真值，用于钉住"任一轮估算即整体估算"。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._raw_calls = 0

    def chat_completion_raw(self, *a, **k):
        self._raw_calls += 1
        raw = super().chat_completion_raw(*a, **k)
        if self._raw_calls >= 2:
            raw["usage"] = {"prompt_tokens": 90, "completion_tokens": 10}
        return raw


class _BudgetExhaustedRunner:
    """桩 Runner：预算耗尽但已产出终答，直吐 AgentResult 以钉住 partial 分支。"""

    def __init__(self, llm_client, tools=None, workspace=None, **kwargs):
        self.llm_client = llm_client
        self.tools = tools
        self.workspace = workspace if workspace is not None else {}
        self.budget = kwargs.get("budget")
        self.on_step = kwargs.get("on_step")

    async def run(self, user_query, target_urls=None):
        self.workspace.setdefault("facts", {
            "https://acme.example.com": [
                {"key": "pricing", "value": "$49/月", "confidence": 0.9,
                 "source_url": "https://acme.example.com"},
            ],
        })
        return AgentResult(
            answer=PARTIAL_ANSWER,
            stop_reason="budget_exceeded",
            steps=[AgentStep(index=1, stop_reason_if_any="budget_exceeded")],
            usage={"prompt_tokens": 800, "completion_tokens": 120, "total": 920},
            usage_estimated=True,
            cost_report=self.budget.snapshot() if self.budget is not None else {},
            elapsed_s=1.0,
            workspace=self.workspace,
        )


def _wire_offline(monkeypatch, tmp_path, llm_client):
    """工作流层零网络隔离：LLM、URL 校验、输出目录、SQLite 全部离线化。"""
    from src.config import settings
    from src.storage import database as dbmod
    from src.workflow import mvp_workflow

    monkeypatch.setattr(mvp_workflow, "get_llm_client", lambda *a, **k: llm_client)
    monkeypatch.setattr(mvp_workflow, "validate_fetch_url", lambda u: u)
    monkeypatch.setattr(mvp_workflow, "validate_fetch_urls", lambda us: list(us))
    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)


def _wire_react_tools(monkeypatch):
    """react 工具链（tool_specs/tools 模块级名字）的触网点隔离。"""
    from src.agent import tools as agent_tools

    monkeypatch.setattr(tool_specs, "get_search_tool", lambda: _FakeSearchTool(list(SEARCH_HIT)))
    monkeypatch.setattr(agent_tools, "validate_fetch_url", lambda u: u)


async def _drive(workflow, user_query="预算耗尽收束调研", **run_kwargs):
    """跑一次工作流并把事件循环让干净——_emit 走 create_task，收尾需显式冲刷。"""
    state = await workflow.run(user_query, **run_kwargs)
    for _ in range(5):
        await asyncio.sleep(0)
    return state


# --------------------------------------------------------------------------
# 终态取值
# --------------------------------------------------------------------------

def test_partial_is_a_declared_task_status():
    assert TaskStatus.PARTIAL.value == "partial"
    assert "partial" in get_args(ResearchState.__annotations__["status"]), \
        "ResearchState.status 的 Literal 必须含 partial"


def test_budget_exceeded_with_answer_is_partial(tmp_path, monkeypatch):
    """预算耗尽 + 有终答 → partial 终态，交付链照常走完。"""
    monkeypatch.setenv("AGENT_MODE", "react")
    _wire_offline(monkeypatch, tmp_path, _react_llm("不会被调用"))
    _wire_react_tools(monkeypatch)

    from src.workflow import mvp_workflow

    monkeypatch.setattr(mvp_workflow, "ReActRunner", _BudgetExhaustedRunner)
    # 引用校验打桩只为了让 passed=True 可确定断言，链路本身仍由真实工作流驱动
    monkeypatch.setattr(mvp_workflow, "verify_report", lambda report, store: {
        "passed": True, "coverage": 1.0, "valid_ratio": 1.0, "grounding": 1.0,
        "violations": [],
    })

    workflow = mvp_workflow.MVPWorkflow()
    collector = _EventCollector()
    workflow._event_emitter = collector

    state = asyncio.run(_drive(
        workflow, enable_search=False, task_id="partial-with-answer-0001",
    ))

    # 终态：partial 而非 completed / failed
    assert state["status"] == "partial", state.get("termination_reason")
    assert state["termination_reason"] == "react:budget_exceeded"

    # 交付链照常：引用校验 passed 落库、报告落盘
    assert state["citation_trusted"] is True, state.get("citation_violations")
    assert state["report_final"] == PARTIAL_ANSWER, "passed 时不应挂低可信前缀"
    files = list((tmp_path / "output").glob("research_report_*.md"))
    assert len(files) == 1, "partial 仍必须落报告"


def test_budget_exceeded_without_answer_still_fails(tmp_path, monkeypatch):
    """预算耗尽 + 无终答 → 仍 failed，partial 只描述"有结论但被截断"。"""
    monkeypatch.setenv("AGENT_MODE", "react")
    llm = _react_llm(
        _call("web_search", query="a"),
        _call("web_search", query="b"),  # 永不产出终答
        _call("web_search", query="c"),
    )
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch)

    from src.workflow import mvp_workflow

    # USD 上限压到 0：第一轮记账后，下一轮预算守卫必然收束
    monkeypatch.setattr(mvp_workflow, "AGENT_USD_BUDGET", 0.0)

    workflow = mvp_workflow.MVPWorkflow()
    collector = _EventCollector()
    workflow._event_emitter = collector

    state = asyncio.run(_drive(
        workflow, enable_search=False, task_id="partial-no-answer-0001",
    ))

    assert state["status"] == "failed", state.get("termination_reason")
    assert state["termination_reason"] == "react:budget_exceeded"
    assert not collector.of("partial"), "无终答不得外发 partial 事件"


def test_normal_halt_still_emits_completed(tmp_path, monkeypatch):
    """对照用例：预算未耗尽的正常收束仍是 completed，防止 partial 分支外溢。"""
    monkeypatch.setenv("AGENT_MODE", "react")
    _wire_offline(monkeypatch, tmp_path, _react_llm("正常终答。"))
    _wire_react_tools(monkeypatch)

    from src.workflow import mvp_workflow

    workflow = mvp_workflow.MVPWorkflow()
    collector = _EventCollector()
    workflow._event_emitter = collector

    state = asyncio.run(_drive(
        workflow, enable_search=False, task_id="partial-control-0001",
    ))

    assert state["status"] == "completed", state.get("termination_reason")
    assert state["termination_reason"] == "react:halt"
    assert len(collector.of("completed")) == 1
    assert not collector.of("partial")


# --------------------------------------------------------------------------
# SSE 事件
# --------------------------------------------------------------------------

def test_partial_event_payload_carries_status_and_cost(tmp_path, monkeypatch):
    """partial 事件带齐 status / termination_reason / stop_reason / cost_report。"""
    monkeypatch.setenv("AGENT_MODE", "react")
    _wire_offline(monkeypatch, tmp_path, _react_llm("不会被调用"))
    _wire_react_tools(monkeypatch)

    from src.workflow import mvp_workflow

    monkeypatch.setattr(mvp_workflow, "ReActRunner", _BudgetExhaustedRunner)
    monkeypatch.setattr(mvp_workflow, "verify_report", lambda report, store: {
        "passed": True, "coverage": 1.0, "valid_ratio": 1.0, "grounding": 1.0,
        "violations": [],
    })

    workflow = mvp_workflow.MVPWorkflow()
    collector = _EventCollector()
    workflow._event_emitter = collector

    state = asyncio.run(_drive(
        workflow, enable_search=False, task_id="partial-event-0001",
    ))

    partial_events = collector.of("partial")
    assert len(partial_events) == 1, [e.event for e in collector.events]
    payload: Dict[str, Any] = partial_events[0].payload
    assert payload["status"] == "partial" == state["status"]
    assert payload["termination_reason"] == "react:budget_exceeded"
    assert payload["stop_reason"] == "budget_exceeded"
    assert payload["cost_report"] == state["cost_report"], "cost_report 必须是本轮 TaskBudget 口径"
    # 该场景不得再出现 completed 事件
    assert not collector.of("completed"), [e.event for e in collector.events]


def test_agent_step_event_payload_has_real_stop_reason(tmp_path, monkeypatch):
    """触发收束的那一轮，agent_step 事件的 stop_reason 必须是真值而非 None。"""
    monkeypatch.setenv("AGENT_MODE", "react")
    monkeypatch.setattr("src.agent.config.AGENT_MAX_STEPS", 8)
    same = _call("web_search", query="acme")
    _wire_offline(monkeypatch, tmp_path, _react_llm(same, same, same, same))
    _wire_react_tools(monkeypatch)

    from src.workflow import mvp_workflow

    workflow = mvp_workflow.MVPWorkflow()
    collector = _EventCollector()
    workflow._event_emitter = collector

    state = asyncio.run(_drive(
        workflow, enable_search=False, task_id="partial-stepreason-0001",
    ))

    assert state["termination_reason"] == "react:duplicate_loop"
    # 每轮 agent_step 带 step/thought/tool_calls/stop_reason，启动播报事件不在此列
    step_events = [e for e in collector.of("agent_step") if "step" in e.payload]
    assert step_events, [e.event for e in collector.events]
    reasons = [e.payload["stop_reason"] for e in step_events]
    assert "duplicate_loop" in reasons, f"收束轮次的 stop_reason 未外发真值: {reasons}"
    assert reasons[-1] == "duplicate_loop", "终止原因应随最后一轮外发"


# --------------------------------------------------------------------------
# stop_reason_if_any 与 usage_estimated
# --------------------------------------------------------------------------

def test_stop_reason_if_any_written_on_budget_exceeded():
    """token 预算在下一轮守卫触发时，终止原因回写到最后一轮 step。"""
    llm = _react_llm(_call("web_search", query="q"), "答案")
    result = asyncio.run(ReActRunner(llm, max_steps=5, token_budget=10).run("t"))

    assert result.stop_reason == "budget_exceeded"
    assert result.steps, "预算超额前应已有 step"
    assert result.steps[-1].stop_reason_if_any == "budget_exceeded", \
        "stop_reason_if_any 恒为 None 是白读，mvp_workflow 靠它外发"


def test_on_step_callback_sees_stop_reason_before_emitting():
    """on_step 回调拿到的 step 必须已经带上终止原因（事件 payload 的来源）。"""
    seen: List[AgentStep] = []
    same = _call("web_search", query="acme")
    llm = _react_llm(same, same, same, same)
    runner = ReActRunner(llm, max_steps=8, on_step=seen.append)

    result = asyncio.run(runner.run("t"))

    assert result.stop_reason == "duplicate_loop"
    assert seen[-1].stop_reason_if_any == "duplicate_loop", "外发时尚未写入终止原因"


def test_usage_estimated_true_without_provider_usage():
    """fake provider 不返回 usage → 只能估算，必须打 estimated 标记。"""
    llm = _react_llm(_call("web_search", query="q"), "答案")
    result = asyncio.run(ReActRunner(llm, max_steps=5).run("t"))

    assert result.usage_estimated is True
    assert result.usage["total"] > 0


def test_usage_estimated_false_with_provider_usage():
    """provider 返回真值 usage → 不得再谎报估算。"""
    llm = _UsageLLM(script={"react": ["终答。"]})
    result = asyncio.run(ReActRunner(llm, max_steps=3).run("t"))

    assert result.usage_estimated is False
    assert result.usage["prompt_tokens"] == 120
    assert result.usage["completion_tokens"] == 30


def test_usage_estimated_true_when_any_round_lacks_usage():
    """部分轮次缺 usage 时整体仍算估算（口径对齐 TaskBudget["estimated"]）。"""
    llm = _MixedUsageLLM(script={"react": [
        _call("web_search", query="a"),
        "答案",
    ]})
    result = asyncio.run(ReActRunner(llm, max_steps=4).run("t"))

    assert result.stop_reason == "halt"
    assert result.usage_estimated is True, "有一轮走估算就不能整体报真值"


# --------------------------------------------------------------------------
# 端到端验收：真实 ReActRunner（不桩）
#
# 上面所有 partial 用例都靠 _BudgetExhaustedRunner 桩件驱动。这条不用桩：
# 它是「M6 PARTIAL 到底能不能真的发生」的最终答案。此前真实 runner 下
# 「预算超限且有终答」永远不成立——预算守卫在每轮开始前就 break 了，
# 而 answer 只在终答分支赋值后立刻 break 成 halt。预算收敛调用补的就是这一段。
# --------------------------------------------------------------------------

def test_real_runner_delivers_partial_when_budget_runs_out(tmp_path, monkeypatch):
    from src.agent import config
    from src.workflow import mvp_workflow

    monkeypatch.setenv("AGENT_MODE", "react")
    # 第 1 轮调工具把事实采回来；第 2 轮守卫立刻判定超额，然后走收敛调用
    llm = _react_llm(
        _call("web_search", query="q"),
        "## 部分结论\n\n基于预算耗尽前已采集的信息。未覆盖：性能对比。",
    )
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch)
    # 把 runner 的 token 预算压到必然超额
    monkeypatch.setattr(config, "AGENT_TOKEN_BUDGET", 10)
    monkeypatch.setattr(mvp_workflow, "verify_report", lambda report, store: {
        "passed": True, "coverage": 1.0, "valid_ratio": 1.0, "grounding": 1.0,
        "violations": [],
    })

    workflow = mvp_workflow.MVPWorkflow()
    state = asyncio.run(_drive(workflow))

    assert state["termination_reason"] == "react:budget_exceeded"
    assert state["status"] == "partial", (
        f"真实 runner 下预算耗尽必须落 partial，实际 {state['status']!r}"
    )
    assert state["report_final"], "partial 也必须交付报告，不能是空壳"
    assert state["report_id"], "partial 报告同样要落盘并有 report_id"


def test_real_runner_emits_partial_event_not_completed(tmp_path, monkeypatch):
    from src.agent import config
    from src.workflow import mvp_workflow

    monkeypatch.setenv("AGENT_MODE", "react")
    llm = _react_llm(_call("web_search", query="q"), "## 部分结论\n\n预算耗尽前采集。")
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch)
    monkeypatch.setattr(config, "AGENT_TOKEN_BUDGET", 10)
    monkeypatch.setattr(mvp_workflow, "verify_report", lambda report, store: {
        "passed": True, "coverage": 1.0, "valid_ratio": 1.0, "grounding": 1.0,
        "violations": [],
    })

    collector = _EventCollector()
    workflow = mvp_workflow.MVPWorkflow(event_emitter=collector)
    asyncio.run(_drive(workflow))

    assert len(collector.of("partial")) == 1, "预算耗尽应收束出 partial 事件"
    assert collector.of("completed") == [], "partial 场景不得再发 completed——那是在撒谎"
    payload = collector.of("partial")[0].payload
    assert payload["status"] == "partial"
    assert payload["stop_reason"] == "budget_exceeded"
