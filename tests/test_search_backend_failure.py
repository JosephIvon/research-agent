"""搜索后端"整轮没结果"必须与"这个查询没结果"分开。

实测事故（2026-10-29 第五轮实测）：SearXNG 的引擎集体超时/被限流时，
整轮返回 0 条；调用方把它当成"没搜到"，于是候选池归零、整组结论作废，
还看不出原因——把后端故障写成了"这个赛道没有竞品"。

判定线（决策契约）：一次结果为空、且引擎集体无响应 → 这是**本轮数据无效**，
不是"0 家竞品"。工装要把这类组重跑，而不是把 0 写进结论。
"""

import asyncio
import json

from src.tools.search_tool import SearchBackendError, SearchTool

import pytest


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _tool_with(monkeypatch, responses):
    """responses: 依次返回的 (payload 或 异常)。"""
    calls = []

    def _fake_get(url, params=None, timeout=None):
        calls.append(params)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)

    monkeypatch.setattr("src.tools.search_tool.requests.get", _fake_get)
    monkeypatch.setattr("src.tools.search_tool.time.sleep", lambda *_: None)
    tool = SearchTool()
    return tool, calls


def test_empty_results_with_unresponsive_engines_raise_a_backend_error(monkeypatch):
    payload = {
        "results": [],
        "unresponsive_engines": ["duckduckgo timeout", "google cse Suspended: too many requests"],
    }
    tool, calls = _tool_with(monkeypatch, [payload])
    with pytest.raises(SearchBackendError) as excinfo:
        tool.search_with_searxng("猫箱 竞品")
    assert "unresponsive" not in str(excinfo.value)
    assert "duckduckgo timeout" in str(excinfo.value)
    assert len(calls) == 2, "要重试一次再判失败"


def test_empty_results_without_engine_problems_are_a_normal_empty_result(monkeypatch):
    """所有引擎都正常应答，只是这个查询没有结果——那是真的没有，不该报错。"""
    tool, calls = _tool_with(monkeypatch, [{"results": [], "unresponsive_engines": []}])
    assert tool.search_with_searxng("asdkjhaskdjh") == []
    assert len(calls) == 1, "没有引擎故障就不用重试"


def test_a_request_failure_is_reported_as_a_backend_error(monkeypatch):
    tool, calls = _tool_with(monkeypatch, [RuntimeError("connection reset")])
    with pytest.raises(SearchBackendError):
        tool.search_with_searxng("猫箱 竞品")
    assert len(calls) == 2


def test_successful_results_are_returned_without_retry(monkeypatch):
    payload = {"results": [{"url": "https://maoxiangai.com/", "title": "猫箱", "content": "x"}],
               "unresponsive_engines": ["duckduckgo timeout"]}
    tool, calls = _tool_with(monkeypatch, [payload])
    results = tool.search_with_searxng("猫箱 竞品")
    assert len(results) == 1
    assert len(calls) == 1, "有结果就不该重试"


def test_the_stage_runner_marks_a_failed_search_instead_of_reporting_zero(monkeypatch):
    """阶段跑完要留下"本轮数据无效"的标记，不能只有 0 家候选。"""
    from src.workflow import search_stages as ss

    class _FailingSearch:
        def search(self, query, num_results=None):
            raise SearchBackendError("搜索引擎无响应（duckduckgo timeout）")

    runner = ss.SearchStageRunner(_FailingSearch())
    plan = _stub_plan()
    runner.run_entity_anchor(plan)
    result = runner.run_competitor_discovery(plan)
    assert result.search_failed is True
    assert result.search_failures
    assert any("搜索后端失败" in note for note in result.notes)
    assert result.to_dict()["search_failed"] is True


def test_a_normal_empty_search_is_not_marked_as_a_backend_failure(monkeypatch):
    from src.workflow import search_stages as ss

    class _EmptySearch:
        def search(self, query, num_results=None):
            return []

    runner = ss.SearchStageRunner(_EmptySearch())
    plan = _stub_plan()
    runner.run_entity_anchor(plan)
    result = runner.run_competitor_discovery(plan)
    assert result.search_failed is False
    assert result.candidates == []


def _stub_plan():
    from src.workflow.research_plan import ResearchPlan, SearchStage

    plan = ResearchPlan()
    plan.entity_name = "猫箱"
    plan.entity_type = "app"
    plan.what_it_is = "AI 陪伴应用"
    plan.resolved_confidence = 0.9
    plan.criteria = {"admission_rule": "必须有官网", "layers": ["direct"],
                      "min_count": 3, "max_count": 5}
    plan.search_plan = [
        SearchStage(stage="entity_anchor", queries=["猫箱 官网"]),
        SearchStage(stage="competitor_discovery", queries=["猫箱 竞品"]),
    ]
    plan.competitor_candidates = []
    return plan
