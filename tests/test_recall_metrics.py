"""发现召回度量的单测（不联网，用构造的 run 记录）。

这些用例锁住的是**口径**，不是某次实跑的数字：
分母怎么算、层与层怎么收窄、丢失归因归到哪一步、分母为 0 时不判。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools import recall_metrics as rm

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "evals" / "discovery_golden" / "companion_apps.json"


def _golden(entries):
    return rm.GoldenSet(entries)


def _run(candidates, scored, selected):
    return {"candidates": candidates, "scored": scored, "selected": selected}


def test_golden_set_file_is_valid_and_has_evidence():
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    for subject, block in data["subjects"].items():
        for entry in block["golden"]:
            assert entry.get("evidence"), f"{subject}/{entry.get('name')} 缺证据"
            assert entry.get("verified_at"), f"{subject}/{entry.get('name')} 缺核实时间"
            assert entry.get("hosts"), f"{subject}/{entry.get('name')} 缺 hosts"


def test_host_key_strips_www_and_port():
    assert rm.host_key("https://www.xingyeai.com/a") == "xingyeai.com"
    assert rm.host_key("http://m.xingyeai.com:8080/") == "m.xingyeai.com"
    assert rm.host_key("") == ""


def test_golden_matches_subdomain_of_golden_host():
    golden = _golden([{"name": "星野", "hosts": ["xingyeai.com"]}])
    assert golden.match("https://m.xingyeai.com/")["name"] == "星野"
    assert golden.match("https://www.xingyeai.com/")["name"] == "星野"
    # 名字相同但域名不同 → 不命中（不能靠名字配对）
    assert golden.match("https://xingye.com/") is None


def test_golden_does_not_match_unrelated_host():
    golden = _golden([{"name": "星野", "hosts": ["xingyeai.com"]}])
    assert golden.match("https://softonic.cn/xingyeai.com") is None
    assert golden.match("https://xingyeai.com.evil.example/") is None


def test_funnel_recall_is_lower_bound_when_pool_is_small():
    golden = _golden([
        {"name": "A", "hosts": ["a.com"]},
        {"name": "B", "hosts": ["b.com"]},
        {"name": "C", "hosts": ["c.com"]},
        {"name": "D", "hosts": ["d.com"]},
    ])
    run = _run(
        candidates=[{"url": "https://a.com/", "name": "A", "aggregator": False}],
        scored=[{"url": "https://a.com/", "name": "A", "gate_passed": True, "gate_reasons": []}],
        selected=[{"url": "https://a.com/", "name": "A"}],
    )
    m = rm.funnel_metrics(run, golden)
    assert m["golden_total"] == 4
    assert m["counts"] == {"L0": 1, "L1": 1, "L2": 1, "L3": 1}
    assert m["recall"]["L0"] == pytest.approx(0.25)
    # 分层保留率的分母是上一层的命中数：1/1 = 1.0
    assert m["keep"]["L1"] == pytest.approx(1.0)
    assert m["keep"]["L2"] == pytest.approx(1.0)
    assert m["keep"]["L3"] == pytest.approx(1.0)


def test_keep_ratio_denominator_is_previous_level_hits():
    """L1 命中 2 个、L2 命中 1 个 → keep_L2 = 0.5（不是 1/4）。"""
    golden = _golden([
        {"name": "A", "hosts": ["a.com"]},
        {"name": "B", "hosts": ["b.com"]},
        {"name": "C", "hosts": ["c.com"]},
        {"name": "D", "hosts": ["d.com"]},
    ])
    run = _run(
        candidates=[
            {"url": "https://a.com/", "name": "A", "aggregator": False},
            {"url": "https://b.com/", "name": "B", "aggregator": False},
            {"url": "https://c.com/", "name": "C", "aggregator": False},
            {"url": "https://d.com/", "name": "D", "aggregator": False},
        ],
        scored=[
            {"url": "https://a.com/", "name": "A", "gate_passed": True, "gate_reasons": []},
            {"url": "https://b.com/", "name": "B", "gate_passed": False, "gate_reasons": ["规则挡下"]},
        ],
        selected=[{"url": "https://a.com/", "name": "A"}],
    )
    m = rm.funnel_metrics(run, golden)
    assert m["found"] == {"L0": 4, "L1": 4, "L2": 1, "L3": 1}
    assert m["keep"]["L2"] == pytest.approx(0.25)
    assert m["keep"]["L3"] == pytest.approx(1.0)
    # 丢在哪一步：B 是被闸门挡下的
    lost = {item["name"]: item for item in m["lost"]}
    assert lost["B"]["reason"] == rm.LOST_GATE
    assert "规则挡下" in lost["B"]["detail"]


def test_lost_attribution_covers_all_four_causes():
    golden = _golden([
        {"name": "找不到", "hosts": ["miss.com"]},
        {"name": "被过滤", "hosts": ["filtered.com"]},
        {"name": "被闸门挡", "hosts": ["gated.com"]},
        {"name": "被截断", "hosts": ["cut.com"]},
        {"name": "入选了", "hosts": ["kept.com"]},
    ])
    run = _run(
        candidates=[
            {"url": "https://filtered.com/", "name": "被过滤", "aggregator": True, "site_type": "media"},
            {"url": "https://gated.com/", "name": "被闸门挡", "aggregator": False, "site_type": "product_site"},
            {"url": "https://cut.com/", "name": "被截断", "aggregator": False, "site_type": "product_site"},
            {"url": "https://kept.com/", "name": "入选了", "aggregator": False, "site_type": "product_site"},
        ],
        scored=[
            {"url": "https://gated.com/", "name": "被闸门挡", "gate_passed": False, "gate_reasons": ["必须是官网"]},
            {"url": "https://cut.com/", "name": "被截断", "gate_passed": True, "gate_reasons": []},
            {"url": "https://kept.com/", "name": "入选了", "gate_passed": True, "gate_reasons": []},
        ],
        selected=[{"url": "https://kept.com/", "name": "入选了"}],
    )
    m = rm.funnel_metrics(run, golden)
    lost = {item["name"]: item for item in m["lost"]}
    assert lost["找不到"]["reason"] == rm.LOST_NOT_FOUND
    assert lost["被过滤"]["reason"] == rm.LOST_SITE_TYPE
    assert lost["被过滤"]["site_type"] == "media"
    assert lost["被闸门挡"]["reason"] == rm.LOST_GATE
    assert lost["被截断"]["reason"] == rm.LOST_CUT
    assert "入选了" not in lost
    assert m["recall"]["L3"] == pytest.approx(0.2)


def test_zero_denominator_is_not_judged_as_zero():
    """金标准里一个都没命中 → 保留率不判（None），不是 0%。"""
    golden = _golden([{"name": "A", "hosts": ["a.com"]}])
    run = _run(candidates=[], scored=[], selected=[])
    m = rm.funnel_metrics(run, golden)
    assert m["recall"]["L0"] == pytest.approx(0.0)
    assert m["keep"]["L1"] is None
    assert m["keep"]["L2"] is None
    assert m["keep"]["L3"] is None
    assert m["precision"]["L0"] is None


def test_stability_jaccard_between_repeats():
    runs = [
        {"selected": [{"url": "https://a.com/"}, {"url": "https://b.com/"}]},
        {"selected": [{"url": "https://a.com/"}, {"url": "https://c.com/"}]},
    ]
    s = rm.stability_metrics(runs, key="selected")
    assert s["runs"] == 2
    assert s["pairwise_jaccard"] == [pytest.approx(0.3333, abs=1e-4)]
    assert s["union_size"] == 3


def test_stability_of_identical_runs_is_one():
    runs = [{"selected": [{"url": "https://a.com/"}]}] * 3
    s = rm.stability_metrics(runs, key="selected")
    assert s["mean_jaccard"] == pytest.approx(1.0)
    assert s["min_jaccard"] == pytest.approx(1.0)


def test_stability_of_completely_different_runs_is_zero():
    runs = [
        {"selected": [{"url": "https://a.com/"}]},
        {"selected": [{"url": "https://b.com/"}]},
    ]
    s = rm.stability_metrics(runs, key="selected")
    assert s["mean_jaccard"] == pytest.approx(0.0)


def test_verdict_none_is_not_a_pass():
    assert rm.verdict("R1", None, 0.7)["status"] == "不判"
    assert rm.verdict("R1", None, 0.7)["pass"] is None
    assert rm.verdict("R1", 0.8, 0.7)["status"] == "PASS"
    assert rm.verdict("R1", 0.5, 0.7)["status"] == "FAIL"


def test_host_overlap_across_phrasings():
    r = rm.host_overlap(
        ["https://www.xingyeai.com/", "https://t.com/"],
        ["https://xingyeai.com/x", "https://u.com/"],
    )
    assert r["jaccard"] == pytest.approx(0.3333, abs=1e-4)
    assert r["common"] == ["xingyeai.com"]
