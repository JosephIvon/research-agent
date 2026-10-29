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
    (lambda d: d["competitor_criteria"].__setitem__("layers", ["unknown_layer"]), "层级非法"),
    (lambda d: d["competitor_criteria"].__setitem__("min_count", 99), "min_count"),
    (lambda d: d["competitor_criteria"].__setitem__("min_count", 6) or
                d["competitor_criteria"].__setitem__("max_count", 3), "min_count > max_count"),
    (lambda d: d["search_plan"][0].__setitem__("stage", "vibes"), "搜索阶段非法"),
])
def test_invalid_plan_is_rejected(mutate, expect):
    data = _plan_data()
    mutate(data)
    problems = rp.validate_plan_data(data)
    assert any(expect in p for p in problems), problems


@pytest.mark.parametrize("mutate", [
    lambda d: d["entity"].__setitem__("entity_type", "小程序"),
    lambda d: d["angles"][0].__setitem__("angle", "pricing"),
    lambda d: d["angles"][0].__setitem__("priority", "very_high"),
])
def test_paraphrased_enum_is_folded_not_rejected(mutate):
    """模型改写枚举词是同义改写，不是错误——折叠回规范词即可。"""
    data = _plan_data()
    mutate(data)
    assert rp.validate_plan_data(data) == []
    plan = rp.build_plan_from_data(data, "x")
    assert plan.source == "model"


def test_angles_that_fold_to_nothing_are_rejected():
    """一个规范角度都折不出来 = 模型在说完全不同的事 → 退回默认计划。"""
    data = _plan_data()
    data["angles"] = [{"angle": "vibes", "priority": "primary", "reason": ""}]
    problems = rp.validate_plan_data(data)
    assert any("无法归入" in p for p in problems), problems


@pytest.mark.parametrize("raw,expected", [
    ("小程序", "app"), ("application", "app"), ("SaaS", "platform"),
    ("公司", "company"), ("tool", "software"), ("vibes", "unknown"), ("", "unknown"),
])
def test_entity_type_folding(raw, expected):
    assert rp.fold_entity_type(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("pricing", "business"), ("market_positioning", "business"),
    ("technology", "engineering"), ("功能", "feature"),
    ("ux", "product"), ("vibes", None),
])
def test_angle_folding(raw, expected):
    assert rp.fold_angle(raw) == expected


def test_priority_folding_is_neutral_when_unknown():
    assert rp.fold_priority("very_high") == "secondary"
    assert rp.fold_priority("high") == "primary"
    assert rp.fold_priority("ignore") == "ignore"


def test_folded_angle_keeps_highest_priority():
    """多个模型角度折到同一规范角度时，取优先级最高的那个。"""
    data = _plan_data()
    data["angles"] = [
        {"angle": "pricing", "priority": "secondary", "reason": "定价", "page_hints": []},
        {"angle": "market_positioning", "priority": "primary", "reason": "市场", "page_hints": []},
    ]
    plan = rp.build_plan_from_data(data, "x")
    business = next(a for a in plan.angles if a.angle == "business")
    assert business.priority == "primary"
    assert business.reason == "市场"


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
    # 发现阶段的查询词由**代码**拼装，不看模型写的（见 test_discovery_queries_are_deterministic）
    assert plan.queries_for("competitor_discovery") == ["百度搭子 竞品", "百度搭子 类似应用"]
    assert plan.top_angles[0] == "feature"


def test_invalid_plan_falls_back_to_defaults():
    """角度一个都折不出来 → 退回默认计划（主体与口径仍然可用则不必退）。"""
    data = _plan_data()
    data["angles"] = [{"angle": "vibes", "priority": "primary", "reason": ""}]
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
    """发现阶段没有维度数据：门槛只能是"有自己的站点"，不能是"≥2 个维度"。

    拿抓完才知道的规则当发现阶段的门槛，结果是维度数恒为 0 → 一家都选不出来。
    """
    plan = rp.build_default_plan("百度搭子")
    result = rp.admit_competitors(_candidates(), plan)
    names = [item["name"] for item in result["selected"]]
    assert "闲聊AI" in names, "有官网的候选就该入选（维度数此时未知）"
    assert all(item["score"] >= rp.MIN_ADMISSION_SCORE for item in result["selected"])


def test_admission_rejects_candidates_without_own_site():
    plan = rp.build_default_plan("百度搭子")
    result = rp.admit_competitors(
        [{"name": "某资讯站", "url": "", "description": "盘点", "dimension_count": 0}], plan
    )
    assert result["selected"] == []


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


def test_subject_own_page_is_not_a_competitor():
    """主体自己的官方页不是竞品。"""
    plan = rp.build_plan_from_data(_plan_data(), "x")
    stub = _StubSearch({
        "百度搭子 是什么": [
            {"title": "百度搭子官网", "url": "https://dazi.test/", "content": "官方"},
        ],
        "百度搭子 竞品": [
            {"title": "百度搭子 - 官方介绍", "url": "https://dazi.test/", "content": "官方"},
            {"title": "猫箱", "url": "https://maoxiang.test/", "content": "AI 搭子"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    runner.run_entity_anchor(plan)
    result = runner.run_competitor_discovery(plan)
    urls = [item["url"] for item in result.selected]
    assert "https://dazi.test/" not in urls, "主体自己的页不能入选"
    assert any("主体" in note for note in result.notes)


def test_same_name_candidates_keep_the_official_site():
    """同一产品出现在多个页面时，保留官网那份。"""
    plan = rp.build_plan_from_data(_plan_data(), "x")
    stub = _StubSearch({
        "百度搭子 竞品": [
            {"title": "猫箱 - 媒体盘点", "url": "https://zhuanlan.zhihu.com/p/1", "content": "盘点"},
            {"title": "猫箱 - AI 搭子", "url": "https://maoxiang.test/", "content": "AI 搭子"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    result = runner.run_competitor_discovery(plan)
    official = [c for c in result.candidates if c.name == "猫箱" and not c.is_aggregator]
    assert official and official[0].url == "https://maoxiang.test/"


def test_aggregator_only_pool_admits_nothing():
    """候选池只剩媒体/百科页时，按口径不拿它们充数——宁可空手。"""
    plan = rp.build_plan_from_data(_plan_data(), "x")
    stub = _StubSearch({
        "百度搭子 竞品": [
            {"title": "盘点文", "url": "https://zhuanlan.zhihu.com/p/1", "content": "盘点"},
            {"title": "百科词条", "url": "https://baike.baidu.com/item/x", "content": "百科"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    result = runner.run_competitor_discovery(plan)
    assert result.selected == []
    assert any("没有竞品官网" in note for note in result.notes)


def test_stage_results_are_deduped_by_url():
    """同一条查询会返回同一批页面；不去重的话"取前 5 条"会被同一个 URL 占满。"""
    plan = rp.build_plan_from_data(_plan_data(), "x")
    stub = _StubSearch({
        "百度搭子": [
            {"title": "A", "url": "https://a.test/", "content": "a"},
            {"title": "B", "url": "https://b.test/", "content": "b"},
        ],
    })
    runner = ss.SearchStageRunner(stub)
    pages = runner.run_entity_anchor(plan)
    urls = [p["url"] for p in pages]
    assert len(urls) == len(set(urls))


@pytest.mark.parametrize("raw", [
    "{竞品名} 官网", "竞品名 官网", "<竞品名> 功能", "{competitor} 缺点",
])
def test_placeholder_queries_are_expanded(raw):
    plan = rp.build_plan_from_data(_plan_data(), "x")
    plan.search_plan = [
        rp.SearchStage(stage="per_competitor_verify", queries=[raw]),
        rp.SearchStage(stage="counter_evidence", queries=[raw]),
    ]
    runner = ss.SearchStageRunner(_StubSearch({"": []}))
    queries = runner.site_scoped_queries(plan, "猫箱", "per_competitor_verify")
    assert queries
    assert all("竞品名" not in q and "{competitor}" not in q for q in queries)
    assert any("猫箱" in q for q in queries)


def test_generic_base_query_gets_competitor_name():
    """模型给通用词（"官网"）时也要拼上竞品名，否则搜的是字面量。"""
    plan = rp.build_plan_from_data(_plan_data(), "x")
    plan.search_plan = [rp.SearchStage(stage="per_competitor_verify", queries=["官网"])]
    runner = ss.SearchStageRunner(_StubSearch({"": []}))
    queries = runner.site_scoped_queries(plan, "猫箱", "per_competitor_verify")
    assert "官网 猫箱" in queries


def test_stage_names_are_folded_to_canonical_ones():
    """模型写 "discovery"/"competitor_search" 是同义改写，不是错误。"""
    assert rp.fold_stage("entity_anchor") == "entity_anchor"
    assert rp.fold_stage("discovery") == "competitor_discovery"
    assert rp.fold_stage("competitor_search") == "competitor_discovery"
    assert rp.fold_stage("find_competitors") == "competitor_discovery"
    assert rp.fold_stage("verify") == "per_competitor_verify"
    assert rp.fold_stage("counter") == "counter_evidence"
    assert rp.fold_stage("完全不是阶段名") is None


def test_paraphrased_stage_names_still_yield_queries():
    """阶段名对不上 → 0 条查询 → 候选池空 → 整轮空转（实测踩过）。"""
    data = _plan_data()
    data["search_plan"] = [
        {"stage": "discovery", "queries": ["百度搭子 竞品"]},
        {"stage": "anchor", "queries": ["百度搭子 官网"]},
    ]
    plan = rp.build_plan_from_data(data, "找一下百度搭子的全部竞品信息")
    # 发现/锚定两个阶段的查询词由代码拼装（不采信模型写的）
    assert plan.queries_for("competitor_discovery") == ["百度搭子 竞品", "百度搭子 类似应用"]
    assert plan.queries_for("entity_anchor") == ["百度搭子 官网", "百度搭子 App 官网"]


def test_every_stage_always_has_queries():
    """缺查询词的阶段必须用默认查询词补上——空阶段等于这一段不搜。"""
    data = _plan_data()
    data["search_plan"] = [
        {"stage": "entity_anchor", "queries": ["百度搭子 官网"]},
    ]
    plan = rp.build_plan_from_data(data, "找一下百度搭子的全部竞品信息")
    assert plan.queries_for("competitor_discovery"), "竞品发现阶段不能没有查询词"
    assert plan.queries_for("entity_anchor")


# ----------------------------------------------------------------------
# 发现阶段的查询词：代码拼装，不由模型自由发挥
# ----------------------------------------------------------------------

def test_discovery_queries_are_deterministic_across_different_model_output():
    """同一主体，模型每次写的查询词都不同 → 候选池整个换掉。

    实测（2026-10-29，`tool_e2e_plan_variance.py`）：同一主体「猫箱」连生成
    5 次计划，查询词两两 Jaccard 只有 0.097，10 对计划无一对逐字相同；
    百度搭子 0.125。计划抽签 → 候选池抽签 → 稳定性线（S1）测的是抽签。

    口径：**模型提名（找谁），代码拼装（怎么搜）**——同主体必得同查询。
    """
    variants = []
    for model_queries in (
        ["百度搭子 AI 官网", "百度搭子 是什么"],
        ["百度搭子 App 官网", "百度搭子 字节"],
        ["百度搭子 官网"],
    ):
        data = _plan_data()
        data["search_plan"] = [
            {"stage": "entity_anchor", "queries": model_queries},
            {"stage": "competitor_discovery", "queries": model_queries},
        ]
        variants.append(rp.build_plan_from_data(data, "找一下百度搭子的全部竞品信息"))
    assert variants[0].queries_for("competitor_discovery") == \
        variants[1].queries_for("competitor_discovery") == \
        variants[2].queries_for("competitor_discovery")
    assert variants[0].queries_for("entity_anchor") == \
        variants[1].queries_for("entity_anchor") == \
        variants[2].queries_for("entity_anchor")


def test_discovery_queries_ignore_what_the_model_wrote():
    """模型给这两个阶段写的查询词**一律不采信**——靠提示词"请别写"是软约束。"""
    data = _plan_data()
    data["search_plan"] = [
        {"stage": "entity_anchor", "queries": ["百度搭子 AI 官网"]},
        {"stage": "competitor_discovery", "queries": ["百度搭子 字节", "百度搭子 开发者"]},
    ]
    plan = rp.build_plan_from_data(data, "x")
    assert plan.queries_for("entity_anchor") == ["百度搭子 官网", "百度搭子 App 官网"]
    assert plan.queries_for("competitor_discovery") == ["百度搭子 竞品", "百度搭子 类似应用"]


def test_verify_and_counter_stages_keep_the_model_templates():
    """逐家验证/反证的查询词是**按竞品展开的意图词**，仍用模型模板。"""
    data = _plan_data()
    data["search_plan"] = [
        {"stage": "per_competitor_verify", "queries": ["{竞品名} 定价"]},
        {"stage": "counter_evidence", "queries": ["{竞品名} 差评"]},
    ]
    plan = rp.build_plan_from_data(data, "x")
    assert plan.queries_for("per_competitor_verify") == ["{竞品名} 定价"]
    assert plan.queries_for("counter_evidence") == ["{竞品名} 差评"]


def test_nominees_are_deduplicated_and_sorted():
    """提名名单：去重 + 排序——顺序不该带随机性（同一份名单要走同一条路）。"""
    data = _plan_data()
    data["competitor_candidates"] = ["筑梦岛", "星野", "星野", "Talkie", "星野 "]
    plan = rp.build_plan_from_data(data, "x")
    assert plan.competitor_candidates == ["Talkie", "星野", "筑梦岛"]


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
        # 锚定阶段的查询词由代码拼装（"百度搭子 官网" / "百度搭子 App 官网"）
        "百度搭子 官网": [
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
    assert restored.queries_for("competitor_discovery") == ["百度搭子 竞品", "百度搭子 类似应用"]


def test_default_plan_has_search_queries():
    """默认计划也得有查询词——否则退回"整句任务描述当搜索词"那条老路。"""
    plan = rp.build_default_plan("找一下百度搭子的全部竞品信息")
    assert plan.queries_for("entity_anchor")
    assert plan.queries_for("competitor_discovery")
    assert "百度搭子 竞品" in plan.queries_for("competitor_discovery")


def test_default_plan_without_entity_has_no_queries():
    plan = rp.build_default_plan("")
    assert plan.queries_for("competitor_discovery") == []


def test_verification_queries_are_bounded():
    plan = rp.build_plan_from_data(_plan_data(), "x")
    stub = _StubSearch({"": []})
    runner = ss.SearchStageRunner(stub)
    runner.run_verification(plan, ["猫箱", "Talkie"])
    assert len(stub.queries) <= 2 * 2 * ss.MAX_QUERIES_PER_STAGE


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
