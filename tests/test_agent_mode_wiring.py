"""M3 工作流接线测试：AGENT_MODE=react 分支、爬虫批次共享、TaskState 新字段。

隔离手法与 test_scripted_llm.py 一致：monkeypatch `mvp_workflow.WebCrawler`、
`mvp_workflow.get_llm_client`、`mvp_workflow.validate_fetch_url/urls`、
`src.config.settings.OUTPUT_DIR`、`src.storage.database` 的 DATABASE_PATH/_report_db。
react 工具链的触网点在 `src.agent.tool_specs` / `src.agent.tools`，单独打补丁。

react 全流程的语义锚点：
- 终答原样成为 report_final（react 路径不经过 CopywriterAgent，无附录/警告前缀）
- cost_report 来自 TaskBudget.snapshot()，fake provider 无 usage → 走估算并打标
- 事件流必须有 agent_step（on_step 回调 → _emit → 事件桩收集）
"""
import asyncio
import json

import pytest

from src.agent import tool_specs
from src.agent import tools as agent_tools
from src.agent.citations import EvidenceStore, make_evidence_id
from src.llm import ScriptedLLMClient
from src.workflow.task_store import (
    InMemoryTaskStore,
    RedisTaskStore,
    TaskEventType,
    TaskState,
    TaskStatus,
)


# --------------------------------------------------------------------------
# 离线替身
# --------------------------------------------------------------------------

SEARCH_HIT = [{"title": "Acme 官网", "url": "https://acme.example.com", "content": "AI 调研工具"}]


class _FakeSearchTool:
    def __init__(self, results):
        self.results = results

    def search(self, query, num_results=None, provider=None):
        return self.results


class _FakeWebCrawler:
    """legacy 爬虫替身：URL 安全校验与抓取本身各有测试，这里只隔离网络。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch_site_profile(self, url, **kwargs):
        return {
            "content": "Acme 官网内容占位",
            "pages": [{"url": url, "content": "Acme 官网内容占位"}],
            "discovered_urls": [],
        }


class _StaticReactCrawler:
    """react 工具链 fetch_site 的替身：单站单页，页面 URL 固定为站点内页。"""

    def __init__(self, page_url):
        self._page_url = page_url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch_site_profile(self, url, **kwargs):
        return {
            "url": url,
            "content": f"{self._page_url} 定价说明正文",
            "pages": [{"url": self._page_url, "content": f"{self._page_url} 定价说明正文"}],
            "discovered_urls": [],
        }


class _EventCollector:
    """事件桩：手动挂在 workflow._event_emitter 上，收集 emit 的事件对象。"""

    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def _wire_offline(monkeypatch, tmp_path, llm_client, crawler_cls=None):
    """工作流层的零网络隔离：LLM、爬虫、URL 校验、输出目录、SQLite 全部离线化。"""
    from src.config import settings
    from src.storage import database as dbmod
    from src.workflow import mvp_workflow

    monkeypatch.setattr(mvp_workflow, "get_llm_client", lambda *a, **k: llm_client)
    monkeypatch.setattr(mvp_workflow, "WebCrawler", crawler_cls or _FakeWebCrawler)
    # URL 校验走透传以保持离线；其真实行为由 test_security_baseline 覆盖
    monkeypatch.setattr(mvp_workflow, "validate_fetch_url", lambda u: u)
    monkeypatch.setattr(mvp_workflow, "validate_fetch_urls", lambda us: list(us))

    monkeypatch.setattr(settings, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)


def _wire_react_tools(monkeypatch, react_crawler_cls=None):
    """react 工具链（tool_specs/tools 模块级名字）的触网点隔离。"""
    monkeypatch.setattr(tool_specs, "get_search_tool", lambda: _FakeSearchTool(SEARCH_HIT))
    if react_crawler_cls is not None:
        monkeypatch.setattr(tool_specs, "WebCrawler", react_crawler_cls)
    # fetch_site 的 url_params 二次 SSRF 校验透传，假域名过不了真实 DNS 校验；
    # 校验行为由 test_unsafe_url_is_blocked + test_security_baseline 单独覆盖
    monkeypatch.setattr(agent_tools, "validate_fetch_url", lambda u: u)


# --------------------------------------------------------------------------
# react 全流程
# --------------------------------------------------------------------------

def test_react_full_flow_completes_with_cost_and_events(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "react")
    llm = ScriptedLLMClient(script={
        "react": [
            {"type": "tool_call", "name": "web_search", "arguments": '{"query": "x"}'},
            "最终结论：无数字句。",
        ],
    })
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch)

    from src.workflow import mvp_workflow
    workflow = mvp_workflow.MVPWorkflow()
    collector = _EventCollector()
    workflow._event_emitter = collector

    state = asyncio.run(workflow.run(
        "react 模式调研",
        enable_search=False,
        deliverables={"report": True, "prd": False},
        task_id="react-e2e-0001",
    ))

    assert state["status"] == "completed", state.get("termination_reason")
    # react 路径的终答原样成为报告，不经过 CopywriterAgent 的前缀/附录加工
    assert state["report_final"] == "最终结论：无数字句。"
    assert state["termination_reason"] == "react:halt"

    # 成本报告来自 TaskBudget.snapshot()：有预算就有账，fake 无 usage 走估算
    cost = state["cost_report"]
    assert cost, "react 路径必须落成本报告"
    assert cost["calls"] >= 1
    assert cost["estimated"] is True

    # on_step 回调把每轮工具调用外发为 agent_step 事件
    assert any(e.event == "agent_step" for e in collector.events)
    assert state["injection_findings_count"] == 0

    # 报告确实落盘
    files = list((tmp_path / "output").glob("research_report_*.md"))
    assert len(files) == 1 and "最终结论" in files[0].read_text(encoding="utf-8")


def test_react_flow_with_citation_trusted(tmp_path, monkeypatch):
    """react 证据链闭环：fetch_site → extract_facts → 终答带预计算 [e:id] 引用。"""
    monkeypatch.setenv("AGENT_MODE", "react")
    site_url = "https://react.example.com"
    # react 的 extract 走页级 source_url（map_reduce_extract 用 page["url"] 回填）
    page_url = f"{site_url}/pricing"
    fact_value = "标准版 $49/席位/月"
    evidence_id = make_evidence_id(page_url, "pricing", fact_value)
    report_md = f"## 定价\n\n标准版 $49/席位/月 [e:{evidence_id}]。\n"

    llm = ScriptedLLMClient(script={
        "react": [
            {"type": "tool_call", "name": "fetch_site",
             "arguments": json.dumps({"url": site_url})},
            {"type": "tool_call", "name": "extract_facts",
             "arguments": json.dumps({"url": site_url})},
            report_md,
        ],
        "extract": [json.dumps([{"key": "pricing", "value": fact_value, "confidence": 0.9}])],
    })
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch, react_crawler_cls=lambda: _StaticReactCrawler(page_url))

    from src.workflow import mvp_workflow
    workflow = mvp_workflow.MVPWorkflow()

    state = asyncio.run(workflow.run(
        "react 定价调研",
        target_urls=[site_url],
        enable_search=False,
        task_id="react-cite-0001",
    ))

    assert state["status"] == "completed", state.get("termination_reason")
    assert state["citation_trusted"] is True, state.get("citation_violations")
    assert state["report_final"] == report_md, "校验通过的报告不应被挂低可信前缀"


def test_react_without_final_answer_fails_with_max_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "react")
    # 3 轮全部 tool_call（query 互不相同，避开重复调用收束）→ 步数耗尽
    monkeypatch.setattr("src.agent.config.AGENT_MAX_STEPS", 3)
    llm = ScriptedLLMClient(script={
        "react": [
            {"type": "tool_call", "name": "web_search",
             "arguments": json.dumps({"query": f"q{i}"})}
            for i in range(3)
        ],
    })
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch)

    from src.workflow import mvp_workflow
    workflow = mvp_workflow.MVPWorkflow()

    state = asyncio.run(workflow.run("无终答调研", enable_search=False, task_id="react-max-0001"))

    assert state["status"] == "failed"
    assert state["termination_reason"] == "react:max_steps"
    assert state["report_final"], "失败也要给一句可读的失败说明"


# --------------------------------------------------------------------------
# legacy 默认路径不受影响
# --------------------------------------------------------------------------

EXTRACT_ITEMS = [
    {"key": "pricing", "value": "定价：标准版 $49/席位/月，企业版 $99/席位/月。", "confidence": 0.9},
    {"key": "features", "value": ["多模型路由", "团队空间", "审计日志"], "confidence": 0.9},
    {"key": "models", "value": ["chat-pro", "chat-lite"], "confidence": 0.9},
    {"key": "target_users", "value": "目标用户：中小团队与独立开发者", "confidence": 0.9},
    {"key": "integrations", "value": "集成生态：飞书、GitHub、Jira", "confidence": 0.9},
    {"key": "limitations", "value": "限制：无私有化部署，审计日志仅企业版可用", "confidence": 0.9},
    {"key": "reviews", "value": "用户评价：口碑整体积极，评测反馈良好", "confidence": 0.9},
]


def test_legacy_mode_is_default_and_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "legacy")
    target_url = "https://acme.example.com/pricing"
    expected_extracted = [dict(item, source_url=target_url) for item in EXTRACT_ITEMS]
    pricing_cite = EvidenceStore.from_extracted_data(expected_extracted).evidences[0].citation
    report_md = (
        "## 定价\n\n"
        f"标准版 $49/席位/月 {pricing_cite}，企业版 $99/席位/月 {pricing_cite}。\n"
    )
    llm = ScriptedLLMClient(script={
        "decompose": ['[{"id": "1", "description": "分析目标网站内容", "priority": 1}]'],
        "extract": [json.dumps(EXTRACT_ITEMS, ensure_ascii=False)],
        "report": [report_md],
    })
    _wire_offline(monkeypatch, tmp_path, llm)

    from src.workflow import mvp_workflow
    workflow = mvp_workflow.MVPWorkflow()
    state = asyncio.run(workflow.run(
        "Acme 定价调研",
        target_urls=[target_url],
        enable_search=False,
        deliverables={"report": True, "prd": False},
        task_id="legacy-e2e-0001",
    ))

    assert state["status"] == "completed", state.get("termination_reason")
    assert state["termination_reason"] == "所有步骤执行完成"
    # legacy 路径不经过 TaskBudget，成本报告保持 None 语义
    assert state.get("cost_report") is None
    assert state["injection_findings_count"] == 0


def test_fetch_all_competitors_shares_one_crawler_instance(tmp_path, monkeypatch):
    """整批共享一个爬虫实例：2 个站点只允许实例化 1 次。"""
    monkeypatch.setenv("AGENT_MODE", "legacy")

    class _CountingCrawler(_FakeWebCrawler):
        instances = 0

        def __init__(self):
            _CountingCrawler.instances += 1

    url_a = "https://aa.example.com/pricing"
    url_b = "https://bb.example.com/pricing"
    cite_a = EvidenceStore.from_extracted_data(
        [dict(item, source_url=url_a) for item in EXTRACT_ITEMS]
    ).evidences[0].citation
    report_md = f"标准版 $49/席位/月 {cite_a}。\n"
    llm = ScriptedLLMClient(script={
        "extract": [json.dumps(EXTRACT_ITEMS, ensure_ascii=False)],
        "report": [report_md],
    })
    _wire_offline(monkeypatch, tmp_path, llm, crawler_cls=_CountingCrawler)

    from src.workflow import mvp_workflow
    workflow = mvp_workflow.MVPWorkflow()
    state = asyncio.run(workflow.run(
        "双站调研",
        target_urls=[url_a, url_b],
        enable_search=False,
        deliverables={"report": True, "prd": False},
        task_id="share-crawler-0001",
    ))

    assert state["status"] == "completed", state.get("termination_reason")
    success = [c for c in state["competitors"] if c["status"] == "success"]
    assert len(success) == 2, [c.get("error_message") for c in state["competitors"]]
    assert _CountingCrawler.instances == 1, "整批必须共享单个爬虫实例"


# --------------------------------------------------------------------------
# TaskState 新字段：序列化往返与后端 update
# --------------------------------------------------------------------------

def test_agent_step_event_type_registered():
    assert TaskEventType.AGENT_STEP.value == "agent_step"


def test_task_state_new_fields_roundtrip():
    ts = TaskState(
        task_id="t1",
        status=TaskStatus.RUNNING,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        user_query="q",
        injection_findings_count=2,
        cost_report={"calls": 3, "estimated_usd": 0.01},
    )
    d = ts.to_dict()
    assert d["injection_findings_count"] == 2
    assert d["cost_report"] == {"calls": 3, "estimated_usd": 0.01}

    restored = TaskState.from_dict(d)
    assert restored.injection_findings_count == 2
    assert restored.cost_report == {"calls": 3, "estimated_usd": 0.01}


def test_task_state_defaults_tolerate_old_payload():
    """旧数据（Redis/内存里的存量状态）没有新字段时反序列化不报错。"""
    old = {
        "task_id": "t2",
        "status": "running",
        "created_at": "x",
        "updated_at": "y",
        "user_query": "q",
    }
    ts = TaskState.from_dict(old)
    assert ts.injection_findings_count == 0
    assert ts.cost_report is None


def test_inmemory_update_supports_new_fields():
    store = InMemoryTaskStore()

    async def _run():
        await store.create("t3", "q")
        updated = await store.update("t3", injection_findings_count=3, cost_report={"a": 1})
        got = await store.get("t3")
        return updated, got

    updated, got = asyncio.run(_run())
    assert updated.injection_findings_count == 3
    assert updated.cost_report == {"a": 1}
    assert got.injection_findings_count == 3
    assert got.cost_report == {"a": 1}


def test_redis_apply_update_fields_supports_new_fields():
    """Redis 后端的字段更新走 _apply_update_fields（不触 Redis，纯函数验证）。"""
    ts = TaskState(
        task_id="t4",
        status=TaskStatus.PENDING,
        created_at="x",
        updated_at="y",
        user_query="q",
    )
    RedisTaskStore._apply_update_fields(
        ts, {"injection_findings_count": 5, "cost_report": {"b": 2}}
    )
    assert ts.injection_findings_count == 5
    assert ts.cost_report == {"b": 2}


def test_workflow_hands_its_budget_to_the_tool_registry(tmp_path, monkeypatch):
    """生产路径（mvp_workflow）必须把 TaskBudget 透给工具闭包。

    变异验证时发现：只在 ReActRunner 层测记账，工作流这一层的透传断了也**不会**
    有任何用例变红——而真实任务走的正是工作流这一层。没有它，extract_facts 的
    逐页 LLM 调用照旧绕开预算账本，token/USD 两道守卫继续按"只有 ReAct 轮"
    的口径放行。
    """
    monkeypatch.setenv("AGENT_MODE", "react")
    site_url = "https://budgetwire.example.com"
    llm = ScriptedLLMClient(script={
        "react": [
            {"type": "tool_call", "name": "fetch_site",
             "arguments": json.dumps({"url": site_url})},
            {"type": "tool_call", "name": "extract_facts",
             "arguments": json.dumps({"url": site_url})},
            "## 结论\n\n定价如上。",
        ],
        "extract": [json.dumps([{"key": "pricing", "value": "$49/月", "confidence": 0.9}])],
    })
    _wire_offline(monkeypatch, tmp_path, llm)
    _wire_react_tools(monkeypatch, react_crawler_cls=lambda: _StaticReactCrawler(f"{site_url}/p"))

    from src.workflow import mvp_workflow
    from src.llm.token_budget import TaskBudget

    captured = {}
    real_builder = mvp_workflow.build_default_tools

    def _spy(llm_client, workspace, budget=None):
        captured["budget"] = budget
        return real_builder(llm_client, workspace, budget=budget)

    monkeypatch.setattr(mvp_workflow, "build_default_tools", _spy)

    workflow = mvp_workflow.MVPWorkflow()
    asyncio.run(workflow.run("接线调研", enable_search=False, task_id="budget-wire-0001"))

    assert "budget" in captured, "工作流根本没调 build_default_tools，用例失去意义"
    assert isinstance(captured["budget"], TaskBudget), (
        f"工作流必须把 TaskBudget 透给工具闭包，实得 {type(captured['budget']).__name__}"
    )
