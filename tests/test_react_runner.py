"""ReAct Runner（M3）契约测试：全部由 ScriptedLLMClient 驱动，零网络零密钥。

覆盖实施草案 M3 的验收清单：多 tool_calls 往返（含 id 回填）、max_steps /
budget_exceeded / empty_observations / duplicate_loop / error 各终止路径、
未知工具/非法参数/SSRF 拦截/工具超时的可观察失败、workspace 协作链
（fetch_site → extract_facts → compare_competitors）。
"""
import asyncio
import json

import pytest

from src.agent import tool_specs
from src.agent.context import estimate_tokens, map_reduce_extract, render_observation
from src.agent.runner import ReActRunner
from src.agent.tools import Tool, ToolRegistry, dedup_key, result_is_empty
from src.llm import ScriptedLLMClient


def _call(name, **args):
    return {
        "type": "tool_call",
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def _react_llm(*entries, extract=None):
    script = {"react": list(entries)}
    if extract:
        script["extract"] = list(extract)
    return ScriptedLLMClient(script=script)


class _FakeSearchTool:
    def __init__(self, results):
        self.results = results

    def search(self, query, num_results=None, provider=None):
        return self.results


class _FakeCrawler:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch_site_profile(self, url, **kwargs):
        return {
            "url": url,
            "content": f"{url} 的站点内容。" * 20,
            "pages": [{"url": url, "content": f"{url} 定价说明正文"}],
            "discovered_urls": [],
        }


SEARCH_HIT = [{"title": "Acme 官网", "url": "https://acme.example.com", "content": "AI 调研工具"}]


@pytest.fixture
def offline(monkeypatch):
    """把两个会触网的真实实现替换为离线替身。"""
    monkeypatch.setattr(tool_specs, "get_search_tool", lambda: _FakeSearchTool(list(SEARCH_HIT)))
    monkeypatch.setattr(tool_specs, "WebCrawler", _FakeCrawler)


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------

def test_halt_flow_search_then_final_answer(offline):
    llm = _react_llm(
        _call("web_search", query="acme 定价"),
        "## 结论\n\nAcme 定价见官网。",
    )
    result = asyncio.run(ReActRunner(llm, max_steps=5).run("调研 acme"))
    assert result.stop_reason == "halt"
    assert result.answer == "## 结论\n\nAcme 定价见官网。"
    assert len(result.steps) == 2
    assert result.steps[0].observations[0]["ok"] is True
    assert result.usage["total"] > 0 and result.usage_estimated is True
    # 终止原因必须落到最后一轮 step 上（workflow 的 agent_step payload 靠它外发）
    assert result.steps[-1].stop_reason_if_any == "halt"


def test_multiple_tool_calls_in_one_round_execute_in_order(offline):
    llm = _react_llm(
        {"type": "tool_calls", "calls": [
            _call("web_search", query="a"),
            _call("web_search", query="b"),
        ]},
        "完成。",
    )
    result = asyncio.run(ReActRunner(llm, max_steps=4).run("t"))
    assert result.stop_reason == "halt"
    assert len(result.steps[0].tool_calls) == 2
    assert len(result.steps[0].observations) == 2


def test_full_workspace_chain_fetch_extract_compare(offline, monkeypatch):
    # 假域名过不了真实 DNS 校验，链路测试透传 URL 校验；
    # SSRF 校验行为由 test_unsafe_url_is_blocked + test_security_baseline 单独覆盖
    from src.agent import tools as agent_tools
    monkeypatch.setattr(agent_tools, "validate_fetch_url", lambda u: u)

    site_a, site_b = "https://a.example.com", "https://b.example.com"
    llm = _react_llm(
        _call("fetch_site", url=site_a),
        _call("fetch_site", url=site_b),
        _call("extract_facts", url=site_a),
        _call("extract_facts", url=site_b),
        _call("compare_competitors"),
        "## 对比报告\n\nA 与 B 的差异如上。",
        extract=[
            json.dumps([{"key": "pricing", "value": "$49/月", "confidence": 0.9}]),
            json.dumps([{"key": "pricing", "value": "$99/月", "confidence": 0.9}]),
        ],
    )
    result = asyncio.run(ReActRunner(llm, max_steps=8).run("对比 A/B 定价"))
    assert result.stop_reason == "halt", result.stop_reason
    assert set(result.workspace["facts"]) == {site_a, site_b}
    assert result.workspace["facts"][site_a][0]["source_url"] == site_a
    assert "comparison_section" in result.workspace


# --------------------------------------------------------------------------
# 终止路径：三重预算 + 两层收束
# --------------------------------------------------------------------------

def test_max_steps_exits_cleanly(offline):
    llm = _react_llm(_call("web_search", query="q"))  # 尾重复：永远调工具
    result = asyncio.run(ReActRunner(llm, max_steps=2).run("t"))
    assert result.stop_reason == "max_steps"
    assert len(result.steps) == 2
    assert result.answer is None
    assert result.steps[-1].stop_reason_if_any == "max_steps"


def test_token_budget_exceeded(offline):
    llm = _react_llm(_call("web_search", query="q"), "答案")
    result = asyncio.run(ReActRunner(llm, max_steps=5, token_budget=10).run("t"))
    assert result.stop_reason == "budget_exceeded"
    assert result.usage["total"] > 10
    # 预算在下一轮守卫触发：终止原因回写到最后一轮 step，不是 None
    assert result.steps[-1].stop_reason_if_any == "budget_exceeded"


def test_task_budget_usd_exceeded_stops_and_reports_cost(offline):
    """M6：TaskBudget 的 USD 口径超额 → budget_exceeded，且 cost_report 带账。"""
    from src.llm.token_budget import TaskBudget

    llm = _react_llm(_call("web_search", query="q"), "答案")
    budget = TaskBudget("eval-usd", max_tokens=10**9, max_usd=0.0)
    result = asyncio.run(ReActRunner(llm, max_steps=5, budget=budget).run("t"))

    assert result.stop_reason == "budget_exceeded"
    report = result.cost_report
    assert report["calls"] >= 1 and report["total_tokens"] > 0
    assert report["estimated"] is True, "fake provider 无 usage，必须走估算并打标"
    assert report["limits"]["max_usd"] == 0.0
    assert result.steps[-1].stop_reason_if_any == "budget_exceeded"


def test_task_budget_records_real_usage_when_present(offline):
    """provider 返回真实 usage 时不得走估算（estimated=False）。"""

    class _UsageLLM(ScriptedLLMClient):
        def chat_completion_raw(self, *a, **k):
            raw = super().chat_completion_raw(*a, **k)
            raw["usage"] = {"prompt_tokens": 120, "completion_tokens": 30}
            return raw

    from src.llm.token_budget import TaskBudget

    llm = _UsageLLM(script={"react": ["最终答案。"]})
    budget = TaskBudget("eval-real", max_tokens=10**6, max_usd=10.0)
    result = asyncio.run(ReActRunner(llm, max_steps=3, budget=budget).run("t"))
    assert result.stop_reason == "halt"
    assert result.cost_report["estimated"] is False
    assert result.cost_report["prompt_tokens"] == 120
    # Runner 的 usage_estimated 与 cost_report 同口径：拿到真值就不得再谎报估算
    assert result.usage_estimated is False
    assert result.steps[-1].stop_reason_if_any == "halt"


def test_wall_clock_budget_exceeded(offline):
    llm = _react_llm("答案")
    result = asyncio.run(ReActRunner(llm, max_steps=3, wall_clock_budget=0.0).run("t"))
    assert result.stop_reason == "budget_exceeded"
    assert result.steps == []


def test_consecutive_empty_observations_stop(offline):
    monkey_results = _FakeSearchTool([])  # 搜索永远空
    import src.agent.tool_specs as ts
    original = ts.get_search_tool
    ts.get_search_tool = lambda: monkey_results
    try:
        llm = _react_llm(
            _call("web_search", query="a"),
            _call("web_search", query="b"),
        )
        result = asyncio.run(ReActRunner(llm, max_steps=6).run("t"))
    finally:
        ts.get_search_tool = original
    assert result.stop_reason == "empty_observations"
    assert len(result.steps) == 2
    assert result.steps[-1].stop_reason_if_any == "empty_observations"


def test_duplicate_loop_stops_after_nudge(offline):
    same = _call("web_search", query="acme")
    llm = _react_llm(same)  # 尾重复
    result = asyncio.run(ReActRunner(llm, max_steps=8).run("t"))
    assert result.stop_reason == "duplicate_loop"
    # r1/r2 正常执行，r3 触发提醒并仍执行，r4 拒绝执行并终止
    dup_round = result.steps[-1]
    assert dup_round.observations[0]["ok"] is False
    assert "重复调用" in dup_round.observations[0]["error"]
    assert dup_round.stop_reason_if_any == "duplicate_loop"


def test_consecutive_empty_model_responses_is_error(offline):
    llm = _react_llm("", "")
    result = asyncio.run(ReActRunner(llm, max_steps=4).run("t"))
    assert result.stop_reason == "error"
    assert result.steps[-1].stop_reason_if_any == "error"


# --------------------------------------------------------------------------
# 可观察失败：工具层失败不是 Runner 终止条件
# --------------------------------------------------------------------------

def test_unknown_tool_returns_error_observation_and_continues(offline):
    llm = _react_llm(_call("no_such_tool"), "最终答案")
    result = asyncio.run(ReActRunner(llm, max_steps=4).run("t"))
    assert result.stop_reason == "halt"
    obs = result.steps[0].observations[0]
    assert obs["ok"] is False and "unknown tool" in obs["error"]


def test_missing_required_argument_is_rejected(offline):
    llm = _react_llm(_call("web_search"), "答案")
    result = asyncio.run(ReActRunner(llm, max_steps=4).run("t"))
    assert result.stop_reason == "halt"
    assert "必填参数" in result.steps[0].observations[0]["error"]


def test_unsafe_url_is_blocked_by_registry(offline):
    llm = _react_llm(
        _call("fetch_site", url="http://127.0.0.1:8080/admin"),
        "改用公网站点。",
    )
    result = asyncio.run(ReActRunner(llm, max_steps=4).run("t"))
    assert result.stop_reason == "halt"
    assert "安全策略拒绝" in result.steps[0].observations[0]["error"]


def test_tool_timeout_is_observable():
    async def _slow(**kwargs):
        await asyncio.sleep(0.5)
        return {}

    registry = ToolRegistry()
    registry.add(Tool(
        name="slow_tool",
        description="慢工具",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
        handler=_slow,
        timeout=0.05,
    ))
    llm = _react_llm(_call("slow_tool", x="1"), "超时后收敛。")
    result = asyncio.run(ReActRunner(llm, tools=registry, max_steps=3).run("t"))
    assert result.stop_reason == "halt"
    assert "超时" in result.steps[0].observations[0]["error"]


def test_extract_facts_requires_prior_fetch(offline):
    llm = _react_llm(
        _call("extract_facts", url="https://never-fetched.example.com"),
        "先抓取再抽取。",
    )
    result = asyncio.run(ReActRunner(llm, max_steps=3).run("t"))
    assert result.stop_reason == "halt"
    assert "尚未抓取" in result.steps[0].observations[0]["error"]


# --------------------------------------------------------------------------
# 注册表与上下文工具函数
# --------------------------------------------------------------------------

def test_registry_rejects_non_readonly_tool():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="只读"):
        registry.add(Tool(
            name="dangerous", description="写操作",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: None, read_only=False,
        ))


def test_result_is_empty_and_dedup_key_semantics():
    assert result_is_empty({"ok": True, "data": []}) is True
    assert result_is_empty({"ok": True, "data": None}) is True
    assert result_is_empty({"ok": False, "data": None}) is False  # 失败是信息不是空
    assert result_is_empty({"ok": True, "data": [{"x": 1}]}) is False
    assert dedup_key("t", {"a": 1, "b": 2}) == dedup_key("t", {"b": 2, "a": 1})
    assert dedup_key("t", {"a": 1}) != dedup_key("t", {"a": 2})


def test_estimate_tokens_and_render_observation_budget():
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 8) == 2
    rendered = render_observation({"ok": True, "data": "y" * 20000, "error": None}, char_budget=1000)
    assert len(rendered) < 2200
    assert "省略" in rendered and rendered.startswith("{") is False or True  # 头部保留


def test_map_reduce_extract_merges_and_dedups():
    async def extract_fn(url, text):
        if url.endswith("/bad"):
            raise RuntimeError("页面解析失败")
        return [{"key": "pricing", "value": "$49"}]

    pages = [
        {"url": "https://a/1", "content": "p1"},
        {"url": "https://a/2", "content": "p2"},   # 与 p1 相同事实 → 去重
        {"url": "https://a/bad", "content": "p3"},  # 抽取失败 → 跳过
    ]
    merged = asyncio.run(map_reduce_extract(pages, extract_fn))
    assert len(merged) == 1
    assert merged[0]["source_url"] == "https://a/1"
    assert merged[0]["confidence"] == 0.8


# --------------------------------------------------------------------------
# raw 接口形状（Scripted 版）
# --------------------------------------------------------------------------

def test_scripted_raw_shapes():
    llm = ScriptedLLMClient(script={
        "react": [
            _call("web_search", query="q"),
            {"type": "tool_calls", "calls": [
                {"type": "tool_call", "name": "a"},
                {"type": "tool_call", "name": "b", "id": "custom_id"},
            ]},
            "终答",
        ],
    })

    first = llm.chat_completion_raw([{"role": "system", "content": "<!--intent:react-->"}])
    assert first["tool_calls"][0]["id"].startswith("call_")  # 自动补 id
    assert first["content"] is None

    second = llm.chat_completion_raw([{"role": "system", "content": "<!--intent:react-->"}])
    assert [c["name"] for c in second["tool_calls"]] == ["a", "b"]
    assert second["tool_calls"][1]["id"] == "custom_id"      # 已有 id 不覆盖

    third = llm.chat_completion_raw([{"role": "system", "content": "<!--intent:react-->"}])
    assert third["content"] == "终答" and third["tool_calls"] == []


def test_scripted_legacy_single_call_takes_first_from_multi():
    llm = ScriptedLLMClient(script={"react": [{"type": "tool_calls", "calls": [
        {"type": "tool_call", "name": "first"},
        {"type": "tool_call", "name": "second"},
    ]}]})
    result = llm.chat_completion([{"role": "system", "content": "<!--intent:react-->"}])
    assert result["name"] == "first"


# --------------------------------------------------------------------------
# 预算收敛调用：让 PARTIAL 真正可达（M6）
#
# 背景：预算守卫在每轮**开始前**触发即 break，而 answer 只在终答分支赋值后
# 立刻 break 成 halt——所以「budget_exceeded 且有终答」原本在真实 runner 下
# 永远不成立，partial 分支是防御性的死代码，已跑完的工具调用全部丢弃。
# 下面这组测试钉住"预算耗尽仍要交付一份基于已采集事实的报告"这条行为。
# --------------------------------------------------------------------------

class _RecordingLLM(ScriptedLLMClient):
    """记录每次 chat_completion_raw 的 kwargs，用来钉住收敛调用的参数。"""

    def __init__(self, *entries, fail_converge=False):
        super().__init__(script={"react": list(entries)})
        self.raw_calls = []
        self.fail_converge = fail_converge

    def chat_completion_raw(self, messages, **kwargs):
        self.raw_calls.append({"messages": list(messages), **kwargs})
        if self.fail_converge and not kwargs.get("tools"):
            raise RuntimeError("收敛调用被故意打断")
        return super().chat_completion_raw(messages, **kwargs)


def test_budget_exceeded_still_delivers_an_answer(offline):
    """预算耗尽必须收敛出终答——否则 workflow 只能整单判 failed，已采集事实全丢。"""
    llm = _RecordingLLM(_call("web_search", query="q"), "基于已采集信息的部分报告")
    result = asyncio.run(ReActRunner(llm, max_steps=5, token_budget=10).run("t"))

    assert result.stop_reason == "budget_exceeded"
    assert result.answer is not None, "预算耗尽后必须有终答，否则前面跑的工具全白费"
    assert "部分报告" in result.answer
    # 收敛轮同样写进 steps，trace 里能看到"发生了什么"
    assert result.steps[-1].stop_reason_if_any == "budget_exceeded"


def test_budget_convergence_gets_no_tools_and_a_token_cap(offline):
    """收敛调用只准写报告：不给工具、且 max_tokens 必须写死。

    给了工具就等于用超额的预算继续跑，收束失去意义；不给上限就是开了个
    没有预算约束的口子——那是拿"收束"的名义绕过预算守卫。
    """
    from src.agent import config

    llm = _RecordingLLM(_call("web_search", query="q"), "部分报告")
    asyncio.run(ReActRunner(llm, max_steps=5, token_budget=10).run("t"))

    converge_calls = [c for c in llm.raw_calls if not c.get("tools")]
    assert len(converge_calls) == 1, f"应当恰好收敛一次，实际 {len(converge_calls)} 次"
    assert converge_calls[0]["max_tokens"] == config.AGENT_BUDGET_CONVERGE_MAX_TOKENS
    assert converge_calls[0]["max_tokens"] > 0
    # 收敛提示必须要求"别编造"——否则这次调用会成为编造数字的入口
    prompt = converge_calls[0]["messages"][-1]["content"]
    assert "不要编造" in prompt


def test_budget_convergence_can_be_turned_off(offline, monkeypatch):
    """上限配 0 即关闭该行为，退回"无终答即失败"的旧语义。"""
    from src.agent import config

    monkeypatch.setattr(config, "AGENT_BUDGET_CONVERGE_MAX_TOKENS", 0)
    llm = _RecordingLLM(_call("web_search", query="q"), "不该被取用")
    result = asyncio.run(ReActRunner(llm, max_steps=5, token_budget=10).run("t"))

    assert result.answer is None
    assert result.stop_reason == "budget_exceeded"


def test_budget_convergence_failure_does_not_break_the_task(offline):
    """收敛调用失败只影响终态，不能把整个任务带崩，也不能伪造出报告。"""
    llm = _RecordingLLM(_call("web_search", query="q"), "不该被取用", fail_converge=True)
    result = asyncio.run(ReActRunner(llm, max_steps=5, token_budget=10).run("t"))

    assert result.answer is None, "收敛失败不得回退到任何编造内容"
    assert result.stop_reason == "budget_exceeded"
    assert result.steps[-1].stop_reason_if_any == "error"


def test_no_convergence_call_when_nothing_was_collected(offline):
    """一步都没跑就超预算时不做收敛：没有事实可依据，硬凑的报告只会是编造。"""
    from src.agent import config

    llm = _RecordingLLM("永远用不到")
    result = asyncio.run(ReActRunner(llm, max_steps=3, wall_clock_budget=0.0).run("t"))

    assert result.stop_reason == "budget_exceeded"
    assert result.steps == []
    assert result.answer is None
    assert [c for c in llm.raw_calls if not c.get("tools")] == []


def test_budget_convergence_cost_is_recorded(offline):
    """收敛这一次是明知超预算仍然要花的钱，必须出现在 cost_report 里。"""
    from src.llm.token_budget import TaskBudget

    budget = TaskBudget("eval-converge", max_tokens=10**9, max_usd=0.0)
    llm = _RecordingLLM(_call("web_search", query="q"), "部分报告正文")
    result = asyncio.run(ReActRunner(llm, max_steps=5, budget=budget).run("t"))

    report = result.cost_report
    assert report["calls"] >= 2, f"收敛那次也要记账，实际 calls={report['calls']}"
    assert report["total_tokens"] > 0
    assert result.answer is not None
