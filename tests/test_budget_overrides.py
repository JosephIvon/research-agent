"""预算可调：用户界面说「请提高预算后重跑」，此前没有任何地方能调。

四道闸门此前只有环境变量一个入口：TaskRequest 没有预算字段，MVPWorkflow 只
能注入 token_budget，Runner 的步数与墙钟永远取默认。而 partial 的提示文案
无条件告诉用户「提高预算后重跑」——指着一个不存在的入口。

这组用例钉住完整链路：
  TaskRequest 预算字段 → MVPWorkflow 注入 → TaskBudget 与 Runner 拿到同一份值
同时钉住「不传就用服务端默认」，别让默认值被悄悄改掉。
"""
import asyncio

import pytest

from src.agent.runner import AgentResult, AgentStep
from src.llm import ScriptedLLMClient
from src.workflow.mvp_workflow import MVPWorkflow


class _CapturingRunner:
    """桩 Runner：只记录构造参数，用来验证预算真的传下去了。"""

    captured = {}

    def __init__(self, llm_client, tools=None, workspace=None, **kwargs):
        _CapturingRunner.captured = dict(kwargs)
        self.llm_client = llm_client
        self.tools = tools
        self.workspace = workspace if workspace is not None else {}
        self.budget = kwargs.get("budget")

    async def run(self, user_query, target_urls=None):
        return AgentResult(
            answer="## 结论\n\n预算够用。",
            stop_reason="completed",
            steps=[AgentStep(index=1)],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total": 20},
            usage_estimated=True,
            cost_report=self.budget.snapshot() if self.budget is not None else {},
            elapsed_s=1.0,
            workspace=self.workspace,
        )


def _wire(monkeypatch, tmp_path):
    from src.agent import config
    from src.workflow import mvp_workflow
    from tests.test_partial_status import _wire_offline, _wire_react_tools

    monkeypatch.setenv("AGENT_MODE", "react")
    llm = ScriptedLLMClient(script={"react": ["占位，本用例用桩 Runner"]})
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch)
    monkeypatch.setattr(mvp_workflow, "ReActRunner", _CapturingRunner)
    monkeypatch.setattr(mvp_workflow, "verify_report", lambda report, store: {
        "passed": True, "coverage": 1.0, "valid_ratio": 1.0, "grounding": 1.0,
        "violations": [],
    })
    return config


# ---------------------------------------------------------------------------
# 请求体层
# ---------------------------------------------------------------------------

def test_task_request_accepts_budget_overrides():
    from server import TaskRequest

    req = TaskRequest(
        query="对比 Acme 与 Beta 的定价",
        max_steps=20,
        token_budget=200000,
        wall_clock_budget=600,
        usd_budget=2.5,
    )
    assert req.max_steps == 20
    assert req.token_budget == 200000
    assert req.wall_clock_budget == 600
    assert req.usd_budget == 2.5


def test_task_request_budget_is_optional():
    from server import TaskRequest

    req = TaskRequest(query="q")
    assert req.max_steps is None
    assert req.token_budget is None
    assert req.wall_clock_budget is None
    assert req.usd_budget is None


@pytest.mark.parametrize("field,value", [
    ("max_steps", 0),
    ("max_steps", 1000),
    ("token_budget", 10),
    ("wall_clock_budget", 1),
    ("usd_budget", 0),
])
def test_task_request_rejects_absurd_budgets(field, value):
    """越界值必须当场被拒——否则一条请求就能把成本闸门放到天上去。"""
    from pydantic import ValidationError
    from server import TaskRequest

    with pytest.raises(ValidationError):
        TaskRequest(query="q", **{field: value})


# ---------------------------------------------------------------------------
# 贯通层
# ---------------------------------------------------------------------------

def test_injected_budgets_reach_the_runner(monkeypatch, tmp_path):
    """注入的步数/墙钟必须真的传到 Runner——此前它们永远取默认。"""
    from src.workflow import mvp_workflow
    from tests.test_partial_status import _drive

    _wire(monkeypatch, tmp_path)
    _CapturingRunner.captured = {}

    workflow = MVPWorkflow(
        llm_client=ScriptedLLMClient(script={"react": ["占位"]}),
        max_steps=25,
        token_budget=333000,
        wall_clock_budget=900,
        usd_budget=3.5,
    )
    asyncio.run(_drive(workflow))

    captured = _CapturingRunner.captured
    assert captured.get("max_steps") == 25, "步数预算没有传到 Runner"
    assert captured.get("wall_clock_budget") == 900, "墙钟预算没有传到 Runner"
    assert captured.get("token_budget") == 333000
    # USD 只走 TaskBudget（Runner 不单独收这个参数）
    assert captured["budget"].max_usd == 3.5, "USD 预算没有进 TaskBudget"
    # token 两道闸门必须同源，否则同一个预算有两个真值源
    assert captured["budget"].max_tokens == 333000
    assert captured.get("token_budget") == captured["budget"].max_tokens


def test_without_overrides_the_runner_keeps_server_defaults(monkeypatch, tmp_path):
    """不传预算时必须落回服务端默认，不能被 None 覆盖成 0。"""
    from src.agent import config
    from tests.test_partial_status import _drive

    _wire(monkeypatch, tmp_path)
    _CapturingRunner.captured = {}

    workflow = MVPWorkflow(llm_client=ScriptedLLMClient(script={"react": ["占位"]}))
    asyncio.run(_drive(workflow))

    captured = _CapturingRunner.captured
    # None = 让 Runner 自己用 config 默认值
    assert captured.get("max_steps") is None
    assert captured.get("wall_clock_budget") is None
    assert captured["budget"].max_usd == config.AGENT_USD_BUDGET
    assert captured["budget"].max_tokens == config.AGENT_TOKEN_BUDGET


def test_settings_payload_exposes_the_budget_defaults():
    """前端要用默认值当占位提示——权威来源必须能取到。"""
    from server import _safe_settings_payload

    payload = _safe_settings_payload()
    assert payload["agent_max_steps"] > 0
    assert payload["agent_token_budget"] > 0
    assert payload["agent_wall_clock_budget"] > 0
    assert payload["agent_usd_budget"] > 0
