"""准入规则：模型写的那句口径，代码真的执行了哪几条。

对应文档：``docs/admission-metric-gap.md`` 第 4 节 (a)(b)(c)。
"""

from src.workflow import admission_rules as ar
from src.workflow import research_plan as rp

import pytest


# -- (c) 规则解析 ---------------------------------------------------------

def test_site_requirement_is_executable():
    rule = ar.parse_admission_rule("必须有官网")
    assert rule.require_product_site is True
    assert rule.source == "model"
    assert any("产品官网" in c for c in rule.executable)


def test_dimension_threshold_is_executable_but_at_the_post_crawl_stage():
    rule = ar.parse_admission_rule("至少有 3 个维度有数据")
    assert rule.min_dimensions == 3
    assert rule.to_dict()["min_dimensions_stage"] == "post_crawl"
    assert any("抓后判定" in c for c in rule.executable)


def test_dimension_threshold_variants():
    assert ar.parse_admission_rule("至少 2 个维度").min_dimensions == 2
    assert ar.parse_admission_rule("≥4 个维度有数据").min_dimensions == 4
    assert ar.parse_admission_rule("不少于 5 个维度").min_dimensions == 5
    assert ar.parse_admission_rule("2 个维度以上").min_dimensions == 2


def test_layer_clause_is_not_executable_but_is_recorded():
    """层归属要语义判断——认得出层名，但代码不据此裁决，也不假装执行了。"""
    rule = ar.parse_admission_rule("直接层须满足个人用户×AI陪伴×移动端App")
    # 整条规则没有一条可执行 → 退回默认口径并标 unparsed（不假装执行了层归属）
    assert rule.source == "unparsed"
    assert "direct" in rule.layers
    assert any("语义判断" in c for c in rule.not_executable)


def test_mixed_rule_is_partial():
    """一部分能执行、一部分不能 → source=partial，两边都记下来。"""
    rule = ar.parse_admission_rule("必须有官网；直接层须满足个人用户×移动端App")
    assert rule.source == "partial"
    assert rule.require_product_site is True
    assert any("产品官网" in c for c in rule.executable)
    assert any("语义判断" in c for c in rule.not_executable)


def test_unparsable_rule_falls_back_to_default_and_is_marked():
    """解析不出来 → 退回默认口径，并如实标记 unparsed（不能静默忽略）。"""
    rule = ar.parse_admission_rule("要跟我们的战略高度契合")
    assert rule.source == "unparsed"
    assert rule.require_product_site is True
    assert rule.min_dimensions == ar.DEFAULT_MIN_DIMENSIONS
    assert rule.not_executable, "解析不出来的条款要留在记录里"


def test_default_site_requirement_is_added_when_rule_does_not_mention_it():
    rule = ar.parse_admission_rule("至少 2 个维度有数据")
    assert rule.require_product_site is True
    assert any("默认口径" in c for c in rule.executable)


def test_structured_include_exclude_are_executable():
    rule = ar.parse_admission_rule(
        "必须有官网",
        {"must_include": ["猫箱"], "must_exclude": ["知乎"]},
    )
    assert "猫箱" in rule.must_include and "知乎" in rule.must_exclude
    assert any("必须包含" in c for c in rule.executable)


# -- 发现阶段的闸门 -------------------------------------------------------

def _cand(name, url, **kw):
    return {"name": name, "url": url, "description": kw.pop("description", ""), **kw}


def test_discovery_gate_rejects_non_product_sites():
    rule = ar.parse_admission_rule("必须有官网")
    ok, reasons = ar.evaluate_discovery(
        _cand("某榜单", "https://zhuanlan.zhihu.com/p/1"), rule
    )
    assert ok is False
    assert reasons and "不是产品官网" in reasons[0]


def test_discovery_gate_passes_product_sites():
    rule = ar.parse_admission_rule("必须有官网")
    ok, reasons = ar.evaluate_discovery(
        _cand("猫箱", "https://maoxiangai.com/"), rule
    )
    assert ok is True and not reasons


def test_discovery_gate_honours_must_exclude():
    rule = ar.parse_admission_rule(
        "必须有官网", {"must_exclude": ["猫箱"]}
    )
    ok, reasons = ar.evaluate_discovery(
        _cand("猫箱", "https://maoxiangai.com/"), rule
    )
    assert ok is False
    assert any("排除项" in r for r in reasons)


def test_admission_records_gate_failures_instead_of_hiding_them():
    """被规则挡下来的候选要留在记录里（gate_failed），否则"为什么没选它"答不了。"""
    plan = rp.build_default_plan("猫箱")
    candidates = [
        _cand("猫箱", "https://maoxiangai.com/", site_type="product_site"),
        _cand("知乎专栏", "https://zhuanlan.zhihu.com/p/1", site_type="media"),
    ]
    result = rp.admit_competitors(candidates, plan)
    assert [c["name"] for c in result["selected"]] == ["猫箱"]
    failed = {item["name"] for item in result["gate_failed"]}
    assert "知乎专栏" in failed
    # 被挡的候选仍在 scored 里（留池要用），但带 gate_passed=False
    scored = {item["name"]: item for item in result["scored"]}
    assert scored["知乎专栏"]["gate_passed"] is False


def test_plan_carries_the_executable_rule():
    """计划要带"可执行形态的规则"，报告/前端才能说清哪几条真的判了。"""
    data = {
        "entity": {"name": "猫箱", "entity_type": "app", "what_it_is": "AI 角色扮演",
                   "resolved_confidence": 0.9},
        "competitor_criteria": {
            "layers": ["direct"], "min_count": 3, "max_count": 5,
            "admission_rule": "必须有官网 + 至少 2 个维度有数据",
        },
        "angles": [{"angle": "feature", "priority": "primary", "reason": "r"}],
        "search_plan": [{"stage": "entity_anchor", "queries": ["猫箱 官网"]}],
    }
    plan = rp.build_plan_from_data(data, "猫箱的竞品")
    rule = plan.to_dict()["admission_rule"]
    assert rule["require_product_site"] is True
    assert rule["min_dimensions"] == 2
    assert rule["min_dimensions_stage"] == "post_crawl"
    assert rule["executable"]


# -- 抓后阶段的闸门 -------------------------------------------------------

def _comp(name, dims):
    return {
        "name": name,
        "url": f"https://{name}.test/",
        "extracted_data": [
            {"key": key, "value": f"{key} 的值"} for key in dims
        ],
    }


def test_post_crawl_gate_eliminates_competitors_with_too_few_dimensions():
    rule = ar.parse_admission_rule("至少有 2 个维度有数据")
    ok, reasons = ar.evaluate_post_crawl(_comp("猫箱", ["pricing"]), rule)
    assert ok is False
    assert any("维度" in r for r in reasons)


def test_post_crawl_gate_passes_enough_dimensions():
    rule = ar.parse_admission_rule("至少有 2 个维度有数据")
    ok, reasons = ar.evaluate_post_crawl(
        _comp("猫箱", ["pricing", "features"]), rule
    )
    assert ok is True and not reasons


def test_post_crawl_gate_counts_only_structured_facts():
    """兜底原文（extraction_failed）与"未公开"声明不算维度。"""
    rule = ar.parse_admission_rule("至少有 1 个维度有数据")
    comp = {
        "name": "x", "url": "https://x.test/",
        "extracted_data": [
            {"key": "raw", "value": "定价 10 元/月，功能很强", "extraction_failed": True},
            {"key": "pricing", "value": "未公开"},
        ],
    }
    ok, _ = ar.evaluate_post_crawl(comp, rule)
    assert ok is False
