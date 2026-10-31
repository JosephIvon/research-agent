"""多口径（换分母）测量的回归测试。

背景（2026-11-01）：金标准从"抽样 18 个"扩成"赛道全集 300 个"之后，
新旧分母的召回**不能直接相减**——必须在**同一批实跑**上并列算。
`tool_e2e_recall.analyze_denominators` 就是干这个的，
这里锁住它的行为：分层口径自动派生、判定线不变、零分母不判。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.tools import recall_metrics as rm  # noqa: E402
import tool_e2e_recall as tool  # noqa: E402


def _run(urls):
    """造一次最小可用的运行记录：候选 → 裁决 → 入选。"""
    candidates = [{"url": u, "name": u, "site_type": "product_site",
                   "aggregator": False} for u in urls]
    scored = [{"url": u, "name": u, "score": 0.9, "gate_passed": True,
               "gate_reasons": []} for u in urls]
    selected = [{"url": urls[0], "name": urls[0]}] if urls else []
    return {"candidates": candidates, "scored": scored, "selected": selected}


def _golden(entries):
    return rm.GoldenSet(entries)


def test_tier_subsets_are_derived_from_source_tier():
    golden = _golden([
        {"name": "A", "hosts": ["a.com"], "source_tier": "external"},
        {"name": "B", "hosts": ["b.com"], "source_tier": "store_only"},
        {"name": "C", "hosts": ["c.com"], "source_tier": "pipeline"},
        {"name": "D", "hosts": ["d.com"], "source_tier": "sample_verified"},
    ])
    subsets = tool._tier_subsets(golden)
    assert set(subsets) == {"口径：仅独立来源", "口径：独立来源+抽样核验", "口径：全部（全集）"}
    assert len(subsets["口径：仅独立来源"]) == 1
    assert len(subsets["口径：独立来源+抽样核验"]) == 2
    assert len(subsets["口径：全部（全集）"]) == 4


def test_golden_without_tier_has_no_subsets():
    golden = _golden([{"name": "A", "hosts": ["a.com"]}])
    assert tool._tier_subsets(golden) == {}


def test_same_runs_scored_against_two_denominators():
    """同一批实跑换分母：分母变大 → 召回变小，但命中数不变。"""
    run = _run(["https://a.com/", "https://b.com/"])
    small = _golden([{"name": "A", "hosts": ["a.com"]}])
    big = _golden([
        {"name": "A", "hosts": ["a.com"]},
        {"name": "B", "hosts": ["b.com"]},
        {"name": "C", "hosts": ["c.com"]},
        {"name": "D", "hosts": ["d.com"]},
    ])
    out = tool.analyze_denominators([run], {"小口径": small, "大口径": big})
    by_label = {row["label"]: row for row in out}
    assert by_label["小口径"]["golden_total"] == 1
    assert by_label["大口径"]["golden_total"] == 4
    assert by_label["小口径"]["recall_L0"] == 1.0
    assert by_label["大口径"]["recall_L0"] == 0.5
    # 判定线不因换分母而改变
    assert by_label["大口径"]["r1_verdict"]["status"] == "FAIL"
    assert by_label["大口径"]["r1_verdict"]["threshold"] == tool.LINES["R1"][2]


def test_denominator_report_lists_tiers():
    """口径对照表里要能看出每条来自哪一层（口径不完整度）。"""
    run = _run(["https://a.com/"])
    golden = _golden([
        {"name": "A", "hosts": ["a.com"], "source_tier": "external"},
        {"name": "C", "hosts": ["c.com"], "source_tier": "pipeline"},
    ])
    out = tool.analyze_denominators([run], {"全集": golden})
    main = [row for row in out if row["label"] == "全集"][0]
    assert main["tiers"] == {"external": 1, "pipeline": 1}
    # 自动派生的分层口径也在报告里
    labels = {row["label"] for row in out}
    assert any("仅独立来源" in label for label in labels)


def test_zero_denominator_is_not_judged_as_zero():
    """分母为 0 → 召回 None（不判），不能判成 0%。"""
    empty = _golden([])
    out = tool.analyze_denominators([_run([])], {"空口径": empty})
    row = out[0]
    assert row["recall_L0"] is None

