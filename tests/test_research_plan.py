"""澄清与定界层（ResearchPlan）+ 搜索四段式的测试。

这一层是"用户给任务描述 → 系统知道该找什么"的翻译层，
所以测试重点在**边界与降级**：结构不合格的计划必须整份拒收，
约束缺失必须退回默认值，搜索失败必须不阻断链路。
"""
import asyncio
import json

import pytest

from src.workflow import research_plan as rp
from src.workflow import search_stages as ss


def _plan_data(**overrides):
    data = {
        "entity": {
            "name": "百度搭子",
            "entity_type": "app",
            "what_it_is": "面向年轻人的 AI 搭子社交产品",
            "disambiguation_candidates": ["百度搭子App", "百度搭子小程序"],
            "resolved_confidence": 0.82,
        },
        "competitor_criteria": {
            "layers": ["direct"],
            "must_include": [],
            "must_exclude": [],
            "min_count": 3,
            "max_count": 5,
            "admission_rule": "必须有官网 + 至少 2 个维度有数据",
        },
        "angles": [
            {"angle": "feature", "priority": "primary", "reason": "功能差异最关键",
             "page_hints": ["定价", "功能"]},
            {"angle": "business", "priority": "secondary", "reason": "", "page_hints": []},
            {"angle": "engineering", "priority": "ignore", "reason": "", "page_hints": []},
            {"angle": "product", "priority": "secondary", "reason": "", "page_hints": []},
        ],
        "search_plan": [
            {"stage": "entity_anchor", "queries": ["百度搭子 是什么"], "acceptance": "命中官方源"},
            {"stage": "competitor_discovery", "queries": ["百度搭子 竞品"],
             "acceptance": "产出候选池"},
        ],
        "open_questions": [{"question": "要不要含替代方案？", "default": "否"}],
    }
    data.update(overrides)
    return data


# ----------------------------------------------------------------------
# 权重：数值由代码算，不由模型给
# ----------------------------------------------------------------------

def test_default_weights_used_when_no_priority_given():
    weights = rp.resolve_weights({})
    assert weights == {key: w for key, _, w in rp.DEFAULT_ANGLES}
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_priority_multipliers_shift_weight_towards_that_angle():
    weights = rp.resolve_weights({
        "feature": "primary", "business": "secondary",
        "engineering": "secondary", "product": "ignore",
    })
    assert weights["feature"] > rp.DEFAULT_ANGLES[0][2]
    assert weights["product"] < rp.DEFAULT_ANGLES[3][2]
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_model_never_sets_weights_directly():
    """模型给了 weight 也不采纳——权重只由优先级算。"""
    data = _plan_data()
    data["angles"][0]["weight"] = 0.99
    plan = rp.build_plan_from_data(data, "找一下百度搭子的全部竞品信息")
    feature = next(a for a in plan.angles if a.angle == "feature")
    assert feature.weight != 0.99
    assert abs(feature.weight - rp.resolve_weights(
        {a.angle: a.priority for a in plan.angles})["feature"]) < 1e-9


# ----------------------------------------------------------------------
# 结构校验：不合格就整份拒收
# ----------------------------------------------------------------------

@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d["entity"].__setitem__("entity_type", "小程序"), "entity_type"),
    (lambda d: d["competitor_criteria"].__setitem__("layers", ["unknown_layer"]), "层级非法"),
    (lambda d: d["competitor_criteria"].__setitem__("min_count", 99), "min_count"),
    (lambda d: d["competitor_criteria"].__setitem__("min_count", 6) or
                d["competitor_criteria"].__setitem__("max_count", 3), "min_count > max_count"),
    (lambda d: d["angles"][0].__setitem__("angle", "vibes"), "分析角度非法"),
    (lambda d: d["angles"][0].__setitem__("priority", "very_high"), "优先级非法"),
    (lambda d: d["search_plan"][0].__setitem__("stage", "vibes"), "搜索阶段非法"),
])
def test_invalid_plan_is_rejected(mutate, expect):
    data = _plan_data()
    mutate(data)
    problems = rp.validate_plan_data(data)
    assert any(expect in p for p in problems), problems


def test_valid_plan_has_no_problems():
    assert rp.validate_plan_data(_plan_data()) == []


def test_entity_name_is_repaired_not_rejected():
    """主体名缺失可以从用户原话补——这是修复，不是采纳半份计划。"""
    data = _plan_data()
    data["entity"]["name"] = ""
    assert rp.validate_plan_data(data) == []
    plan = rp.build_plan_from_data(data, "找一下百度搭子的全部竞品信息")
    assert plan.entity_name == "百度搭子"


@pytest.mark.parametrize("query,expected", [
    ("找一下百度搭子的全部竞品信息", "百度搭子"),
    ("帮我看看小红书的竞品", "小红书"),
    ("调研猫箱", "猫箱"),
])
def test_entity_name_from_task_sentence(query, expected):
    assert rp._entity_name_from_query(query) == expected


# ----------------------------------------------------------------------
# 计划生成：失败必须降级而不是崩
# ----------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def achat_completion(self, messages, **kwargs):
        self.calls.append(messages)
        if self.error:
            raise RuntimeError(self.error)
        return self.response


def test_build_plan_from_model_output():
    llm = _FakeLLM(response=json.dumps(_plan_data(), ensure_ascii=False))
    plan = asyncio.run(rp.build_research_plan(llm, "找一下百度搭子的全部竞品信息"))
    assert plan.source == "model"
    assert plan.entity_name == "百度搭子"
    assert plan.entity_type == "app"
    assert plan.min_count == 3 and plan.max_count == 5
    assert plan.queries_for("competitor_discovery") == ["百度搭子 竞品"]
    assert plan.top_angles[0] == "feature"


def test_invalid_plan_falls_back_to_defaults():
    data = _plan_data()
    data["angles"][0]["angle"] = "vibes"
    llm = _FakeLLM(response=json.dumps(data, ensure_ascii=False))
    plan = asyncio.run(rp.build_research_plan(llm, "找一下百度搭子的全部竞品信息"))
    assert plan.source == "default"
    assert plan.error
    assert plan.entity_name == "百度搭子"
    assert plan.min_count == rp.DEFAULT_MIN_COMPETITORS


def test_llm_failure_falls_back_without_raising():
    llm = _FakeLLM(error="模型炸了")
    plan = asyncio.run(rp.build_research_plan(llm, "找一下百度搭子的全部竞品信息"))
    assert plan.source == "default"
    assert "模型炸了" in plan.error


def test_fenced_json_plan_is_parsed():
    payload = "```json\n" + json.dumps(_plan_data(), ensure_ascii=False) + "\n```"
    llm = _FakeLLM(response=payload)
    plan = asyncio.run(rp.build_research_plan(llm, "找一下百度搭子的全部竞品信息"))
    assert plan.source == "model"


def test_angle_priority_tiers_only_when_hints_exist():
    plan = rp.build_plan_from_data(_plan_data(), "x")
    tiers = rp.angle_priority_tiers(plan)
    assert tiers and tiers[0][1] == ("定价", "功能")
    plan_no_hints = rp.build_default_plan("x")
    assert rp.angle_priority_tiers(plan_no_hints) is None


# ----------------------------------------------------------------------
# 候选入选：代码裁决，不由模型宣布
# ----------------------------------------------------------------------

def _candidates():
    return [
        {"name": "猫箱", "url": "https://maoxiang.test/", "description": "AI 搭子社交", "dimension_count": 4},
        {"name": "Talkie", "url": "https://talkie.test/", "description": "AI 角色扮演", "dimension_count": 3},
        {"name": "筑梦岛", "url": "https://zhumengdao.test/", "description": "AI 故事", "dimension_count": 2},
        {"name": "闲聊AI", "url": "https://xiaolian.test/", "description": "AI 陪伴", "dimension_count": 1},
    ]


def test_admission_respects_max_count():
    plan = rp.build_default_plan("百度搭子")
    result = rp.admit_competitors(_candidates(), plan)
    assert len(result["selected"]) <= plan.max_count


def test_admission_requires_two_dimensions():
    plan = rp.build_default_plan("百度搭子")
    result = rp.admit_competitors(_candidates(), plan)
    names = [item["name"] for item in result["selected"]]
    assert "闲聊AI" not in names, "只有 1 个维度的候选不该入选"


def test_admission_reports_minimum_shortfall():
    plan = rp.build_default_plan("百度搭子")
    plan.criteria["min_count"] = 4
    result = rp.admit_competitors(_candidates()[:2], plan)
    assert result["met_minimum"] is False


def test_must_include_is_honoured():
    plan = rp.build_default_plan("百度搭子")
    plan.criteria["must_include"] = ["闲聊AI"]
    result = rp.admit_competitors(_candidates(), plan)
    assert "闲聊AI" in [item["name"] for item in result["selected"]]


def test_must_exclude_is_penalised():
    plan = rp.build_default_plan("百度搭子")
    plan.criteria["must_exclude"] = ["猫箱"]
    result = rp.admit_competitors(_candidates(), plan)
    scored = {item["name"]: item for item in result["scored"]}
    assert scored["猫箱"]["score"] < scored["Talkie"]["score"]


# ----------------------------------------------------------------------
# 搜索四段式
# ----------------------------------------------------------------------

class _StubSearch:
    def __init__(self, mapping):
        self.mapping = mapping
        self.queries = []

    def search(self, query, num_results=None, provider=None):
        self.queries.append(query)
        for key, results in self.mapping.items():
            if key in query:
                return list(results)
        return []


def test_aggregator_pages_are_not_treated_as_competitors():
    assert ss.looks_like_aggregator("https://zhuanlan.zhihu.com/p/123")
    assert ss.looks_like_aggregator("https://baike.baidu.com/item/x")
    assert not ss.looks_like_aggregator("https://meicai.com/")


def test_candidate_name_is_cleaned():
    assert ss.clean_candidate_name("猫箱 - AI 搭子", "https://maoxiang.test/") == "猫箱"
    assert ss.clean_candidate_name("", "https://maoxiang.test/x") == "maoxiang"


def test_discovery_filters_aggregators_and_admits_official_sites():
    plan = rp.build_plan_from_data(_plan_data(), "x")
    stub = _StubSearch({
        "百度搭子 竞品": [
            {"title": "猫箱 - AI 搭子", "url": "https://maoxiang.test/", "content": "AI 搭子社交"},
            {"title": "知乎专栏：AI 搭子盘点", "url": "https://zhuanlan.zhihu.com/p/1", "content": "盘点"},
            {"title": "Talkie", "url": "https://talkie.test/", "content": "AI 角色扮演"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    result = runner.run_competitor_discovery(plan)
    urls = [item["url"] for item in result.selected]
    assert "https://zhuanlan.zhihu.com/p/1" not in urls
    assert any("zhuanlan" in note for note in result.notes)


def test_entity_anchor_prefers_official_pages():
    plan = rp.build_plan_from_data(_plan_data(), "x")
    stub = _StubSearch({
        "百度搭子 是什么": [
            {"title": "盘点文", "url": "https://zhuanlan.zhihu.com/p/2", "content": "介绍"},
            {"title": "百度搭子官网", "url": "https://dazi.test/", "content": "官方"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    pages = runner.run_entity_anchor(plan)
    assert pages[0]["url"] == "https://dazi.test/"


def test_verification_queries_are_scoped_per_competitor():
    plan = rp.build_plan_from_data(_plan_data(), "x")
    stub = _StubSearch({"": []})
    runner = ss.SearchStageRunner(stub)
    runner.run_verification(plan, ["猫箱"])
    assert any("猫箱" in q for q in stub.queries)
    assert any("评价" in q or "缺点" in q for q in stub.queries)


def test_search_failure_does_not_raise():
    class Boom:
        def search(self, query, num_results=None, provider=None):
            raise RuntimeError("搜索引擎炸了")

    plan = rp.build_plan_from_data(_plan_data(), "x")
    runner = ss.SearchStageRunner(Boom())
    assert runner.run_competitor_discovery(plan).candidates == []
    assert runner.run_entity_anchor(plan) == []


# ----------------------------------------------------------------------
# 反证页归并：证据页不能自成一行
# ----------------------------------------------------------------------

def _pages():
    return [
        {"stage": "counter_evidence", "competitor": "猫箱", "url": "https://review.test/a"},
        {"stage": "counter_evidence", "competitor": "猫箱", "url": "https://review.test/b"},
        {"stage": "counter_evidence", "competitor": "猫箱", "url": "https://review.test/c"},
        {"stage": "per_competitor_verify", "competitor": "猫箱", "url": "https://info.test/x"},
    ]


def test_counter_evidence_sites_are_bounded_per_competitor():
    from src.workflow.mvp_workflow import MVPWorkflow

    sites = MVPWorkflow._counter_evidence_sites(_pages(), [])
    assert len(sites) == 2, "每家最多两页反证"
    assert all(s["parent_platform"] == "猫箱" for s in sites)
    assert all(s["max_pages"] == 1 for s in sites)


def test_counter_evidence_sites_skip_already_known_urls():
    from src.workflow.mvp_workflow import MVPWorkflow

    sites = MVPWorkflow._counter_evidence_sites(
        _pages(), ["https://review.test/a", "https://review.test/b"]
    )
    assert [s["url"] for s in sites] == ["https://review.test/c"]


def test_evidence_pages_merge_into_parent_competitor():
    from src.workflow.mvp_workflow import MVPWorkflow

    sites = [
        {"url": "https://maoxiang.test/"},
        {"url": "https://review.test/a", "parent_platform": "猫箱"},
    ]
    competitors = [
        {"name": "猫箱", "url": "https://maoxiang.test/", "extracted_data": [{"key": "name", "value": "猫箱"}]},
        {"name": "快驴进货 App", "url": "https://review.test/a", "extracted_data": [{"key": "limitations", "value": "响应慢"}]},
    ]
    merged, orphans = MVPWorkflow._merge_evidence_pages(sites, competitors)
    assert len(merged) == 1, "证据页不能自成一行"
    assert merged[0]["name"] == "猫箱"
    keys = {item["key"] for item in merged[0]["extracted_data"]}
    assert keys == {"name", "limitations"}, "反证证据要并进它所属的竞品行"
    assert not orphans


def test_orphan_evidence_page_is_kept_out_of_competitors():
    from src.workflow.mvp_workflow import MVPWorkflow

    sites = [{"url": "https://review.test/a", "parent_platform": "猫箱"}]
    competitors = [
        {"name": "快驴进货 App", "url": "https://review.test/a", "extracted_data": []},
    ]
    merged, orphans = MVPWorkflow._merge_evidence_pages(sites, competitors)
    assert merged == [], "无主证据页不是竞品"
    assert orphans and orphans[0]["evidence_page_for"] == "猫箱"


# ----------------------------------------------------------------------
# ReAct 工具：verify_entity / discover_competitors
# ----------------------------------------------------------------------

def test_plan_round_trips_through_dict():
    plan = rp.build_plan_from_data(_plan_data(), "x")
    restored = rp.ResearchPlan.from_dict(plan.to_dict())
    assert restored.entity_name == plan.entity_name
    assert restored.entity_type == plan.entity_type
    assert restored.min_count == plan.min_count
    # to_dict 的权重保留 4 位小数（展示口径），还原后允许 1e-4 的差
    assert abs(restored.angle_weights["feature"] - plan.angle_weights["feature"]) < 1e-4
    assert restored.queries_for("competitor_discovery") == ["百度搭子 竞品"]


def test_verify_entity_tool_reports_plan():
    from src.agent.tool_specs import build_default_tools

    plan = rp.build_plan_from_data(_plan_data(), "x")
    workspace = {"research_plan": plan.to_dict()}
    registry = build_default_tools(None, workspace)
    result = asyncio.run(registry.get("verify_entity").handler())
    assert result["resolved"] is True
    assert result["entity_name"] == "百度搭子"
    assert result["competitor_criteria"]["min_count"] == 3
    assert result["angles"][0]["label"]


def test_verify_entity_tool_without_plan_is_honest():
    from src.agent.tool_specs import build_default_tools

    registry = build_default_tools(None, {})
    result = asyncio.run(registry.get("verify_entity").handler())
    assert result["resolved"] is False


def test_discover_competitors_tool_uses_code_admission():
    from src.agent.tool_specs import build_default_tools

    plan = rp.build_plan_from_data(_plan_data(), "x")
    workspace = {"research_plan": plan.to_dict()}

    class _Stub:
        def search(self, query, num_results=None, provider=None):
            if "竞品" in query:
                return [
                    {"title": "猫箱 - AI 搭子", "url": "https://maoxiang.test/", "content": "AI 搭子"},
                    {"title": "盘点文章", "url": "https://zhuanlan.zhihu.com/p/9", "content": "盘点"},
                ]
            return []

    registry = build_default_tools(None, workspace, search_factory=lambda: _Stub())
    result = asyncio.run(registry.get("discover_competitors").handler())
    urls = [item["url"] for item in result["competitors"]]
    assert "https://zhuanlan.zhihu.com/p/9" not in urls, "媒体页不该被当成竞品"
    assert result["admission_rule"], "入选口径要一并返回，模型才知道边界"
    assert workspace["discovered_competitors"] is not None, "结果应被复用"


def test_discover_competitors_tool_caches_result():
    from src.agent.tool_specs import build_default_tools

    plan = rp.build_plan_from_data(_plan_data(), "x")
    workspace = {"research_plan": plan.to_dict()}

    calls = []

    class _Stub:
        def search(self, query, num_results=None, provider=None):
            calls.append(query)
            return []

    registry = build_default_tools(None, workspace, search_factory=lambda: _Stub())
    tool = registry.get("discover_competitors")
    asyncio.run(tool.handler())
    first = len(calls)
    asyncio.run(tool.handler())
    assert len(calls) == first, "第二次调用应复用缓存，不再打搜索"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
