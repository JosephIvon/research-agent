"""决策契约的判定线：Q2 人工核对 + Q4 代理判据加"产品官网"层。

判定线写在 docs/decision-contract-search-e2e-2026-10.md，
这里用离线记录验证**按线判的那段代码**，不联网。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "tool_e2e_search_real", REPO / "tool_e2e_search_real.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["tool_e2e_search_real"] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


def _record(subject="百度搭子", style="task", selected=None):
    return {
        "subject": subject,
        "style": style,
        "query": "找一下百度搭子的全部竞品信息",
        "entity": {"name": subject, "type": "app", "what_it_is": "", "confidence": 0.8,
                   "alternatives": [], "source": "model", "error": ""},
        "criteria": {"layers": ["direct"], "min_count": 3, "max_count": 5, "admission_rule": ""},
        "angles": [],
        "stages": [],
        "candidates": [],
        "selected": selected or [],
        "notes": [],
        "counter_evidence": [],
        "elapsed_s": 1.0,
    }


def _selected(name, url, site_type="product_site", in_track=True):
    return {"name": name, "url": url, "score": 0.5, "reasons": [],
            "site_type": site_type, "in_track": in_track}


def test_q2_stays_pending_without_a_manual_review():
    """代理判定是自证的——没有人工核对就不能给 Q2 下结论。"""
    records = [_record(selected=[_selected("猫箱", "https://maoxiangai.com/")])]
    verdict = TOOL._judge(records)
    assert verdict["Q2"]["pass"] is None
    assert verdict["Q2"]["status"].startswith("待人工核对")
    assert verdict["Q2"]["detail"][0]["manual_ratio"] is None


def test_q2_uses_the_manual_verdicts_when_the_sheet_is_filled():
    records = [_record(selected=[
        _selected("猫箱", "https://maoxiangai.com/"),
        _selected("星野", "https://xingye.example/"),
        _selected("应用商店页", "https://apps.apple.com/app/x"),
        _selected("知乎专栏", "https://zhuanlan.zhihu.com/p/1"),
        _selected("真竞品", "https://real.example/"),
    ])]
    review = {"verdicts": [
        {"url": "https://maoxiangai.com/", "verdict": True},
        {"url": "https://xingye.example/", "verdict": False},
        {"url": "https://apps.apple.com/app/x", "verdict": False},
        {"url": "https://zhuanlan.zhihu.com/p/1", "verdict": False},
        {"url": "https://real.example/", "verdict": True},
    ]}
    verdict = TOOL._judge(records, review)
    item = verdict["Q2"]["detail"][0]
    assert item["manual_checked"] == 5
    assert item["manual_real"] == 2
    assert item["manual_ratio"] == 0.4
    assert verdict["Q2"]["pass"] is False
    assert verdict["Q2"]["status"] == "已人工核对"


def test_q2_passes_at_the_prescribed_80_percent():
    records = [_record(selected=[
        _selected("A", "https://a.example/"),
        _selected("B", "https://b.example/"),
        _selected("C", "https://c.example/"),
        _selected("D", "https://d.example/"),
        _selected("E", "https://e.example/"),
    ])]
    review = {"verdicts": [
        {"url": "https://a.example/", "verdict": True},
        {"url": "https://b.example/", "verdict": True},
        {"url": "https://c.example/", "verdict": True},
        {"url": "https://d.example/", "verdict": True},
        {"url": "https://e.example/", "verdict": False},
    ]}
    verdict = TOOL._judge(records, review)
    assert verdict["Q2"]["detail"][0]["manual_ratio"] == 0.8
    assert verdict["Q2"]["pass"] is True


def test_unfilled_rows_do_not_count_as_checked():
    """没填的行不算"通过"，也不算"不通过"——只是没核对。"""
    records = [_record(selected=[
        _selected("A", "https://a.example/"),
        _selected("B", "https://b.example/"),
    ])]
    review = {"verdicts": [{"url": "https://a.example/", "verdict": True}]}
    verdict = TOOL._judge(records, review)
    item = verdict["Q2"]["detail"][0]
    assert item["manual_checked"] == 1
    assert item["manual_real"] == 1
    assert item["manual_ratio"] == 1.0


def test_q4_requires_both_a_product_site_and_the_track():
    """代理判据加了"必须是产品官网"这一层：文章页即使带赛道词也不算数。"""
    records = [_record(selected=[
        _selected("猫箱", "https://maoxiangai.com/", site_type="product_site"),
        _selected("星野", "https://xingye.example/", site_type="product_site"),
        _selected("15个AI虚拟女友…社交软件", "https://zhuanlan.zhihu.com/p/1",
                  site_type="media", in_track=True),
        _selected("某AI陪伴应用", "https://apps.apple.com/app/x",
                  site_type="app_store", in_track=True),
    ])]
    verdict = TOOL._judge(records)
    item = verdict["Q4"]["detail"][0]
    assert item["in_track"] == 2
    assert item["ratio"] == 0.5
    assert item["pass"] is False


def test_q4_passes_when_three_quarters_are_product_sites_in_track():
    records = [_record(selected=[
        _selected("A", "https://a.example/"),
        _selected("B", "https://b.example/"),
        _selected("C", "https://c.example/"),
        _selected("D", "https://d.example/", site_type="media"),
    ])]
    verdict = TOOL._judge(records)
    assert verdict["Q4"]["detail"][0]["ratio"] == 0.75
    assert verdict["Q4"]["pass"] is True


def test_review_sheet_lists_every_selected_item():
    records = [_record(selected=[
        _selected("猫箱", "https://maoxiangai.com/"),
        _selected("星野", "https://xingye.example/"),
    ])]
    sheet = TOOL._review_sheet(records)
    assert len(sheet["verdicts"]) == 2
    assert all(item["verdict"] is None for item in sheet["verdicts"])
    assert sheet["verdicts"][0]["site_type"] == "product_site"
    # 核对表要能脱离网络复算：原始记录一起带上
    assert sheet["records"][0]["selected"][0]["name"] == "猫箱"
    assert "人工核对表" in TOOL._render_review_sheet(sheet)


def test_q3_jaccard_line_is_unchanged():
    """Q3 的线没动：同一主体两种句式的入选名单 Jaccard ≥ 0.5。"""
    records = [
        _record(style="task", selected=[_selected("A", "https://a.example/"),
                                        _selected("B", "https://b.example/")]),
        _record(style="search", selected=[_selected("A", "https://a.example/"),
                                          _selected("B", "https://b.example/")]),
    ]
    verdict = TOOL._judge(records)
    assert verdict["Q3"]["detail"][0]["jaccard"] == 1.0
    assert verdict["Q3"]["pass"] is True


def test_q3_does_not_pass_when_both_sides_selected_nothing():
    """两边都空 → Jaccard 算出来是 1.0，但那是**假的 PASS**。

    什么也没选出来，"两种句式一致"这件事没有证据。
    实测里就有整组候选池归零的——那不是"一致"，是"没数据"。
    """
    records = [
        _record(style="task", selected=[]),
        _record(style="search", selected=[]),
    ]
    verdict = TOOL._judge(records)
    item = verdict["Q3"]["detail"][0]
    assert item["jaccard"] == 1.0
    assert item["empty"] is True
    assert item["pass"] is False
    assert verdict["Q3"]["pass"] is False
