"""同分块（tie）相关的测试。

背景见 docs/retrieval-literature-review.md §3.1：候选分数几乎全是并列 0.5，
同分块内按名字字母序 → 字母序在决定谁能进名额。评测文献（FRAME'26 @ RecSys'26）
把 tie-break 规则算作排序规则的一部分，所以指标要报"同分下的期望值"。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tools import recall_metrics as rm  # noqa: E402
from src.workflow.research_plan import select_competitors  # noqa: E402


def _scored(items):
    """items: [(name, url, score)] → 已打分候选。"""
    return [
        {"name": n, "url": u, "score": s, "gate_passed": True, "gate_reasons": []}
        for n, u, s in items
    ]


def test_tie_break_name_keeps_legacy_order():
    """生产默认（name）必须与历史行为一致：同分按名字字母序。"""
    scored = _scored([
        ("Kimi", "https://kimi.com/", 0.5),
        ("Alpha", "https://alpha.com/", 0.5),
        ("Zeta", "https://zeta.com/", 0.5),
    ])
    chosen = select_competitors(scored, max_count=2, tie_break="name")
    assert [c["name"] for c in chosen] == ["Alpha", "Kimi"]


def test_tie_break_random_is_reproducible_per_seed():
    """同一种子必须给出同一结果（可复现），换种子才换次序。"""
    scored = _scored([
        (f"P{i}", f"https://p{i}.example.com/", 0.5) for i in range(8)
    ])
    a = [c["name"] for c in select_competitors(scored, 3, tie_break="random", seed=7)]
    b = [c["name"] for c in select_competitors(scored, 3, tie_break="random", seed=7)]
    assert a == b
    c = [c["name"] for c in select_competitors(scored, 3, tie_break="random", seed=8)]
    # 同分块 8 个、只取 3 个 → 换种子应该换出不同的组合
    assert a != c


def test_tie_break_modes_agree_on_scores_and_hosts():
    """分数更高的必须先进；同主机只算一家——这两条与 tie-break 无关。"""
    scored = _scored([
        ("High", "https://high.example.com/", 0.75),
        ("Low", "https://low.example.com/", 0.5),
        ("SameHost", "https://high.example.com/about", 0.5),
    ])
    for mode in ("name", "hash", "random"):
        chosen = select_competitors(scored, 3, tie_break=mode, seed=1)
        names = [c["name"] for c in chosen]
        assert names[0] == "High", mode
        assert "SameHost" not in names, mode


def test_unknown_tie_break_mode_raises():
    scored = _scored([("A", "https://a.example.com/", 0.5)])
    with pytest.raises(ValueError):
        select_competitors(scored, 1, tie_break="whatever")


def test_tie_stats_reports_tied_ratio_and_boundary_crossing():
    scored = _scored([
        ("A", "https://a.example.com/", 0.5),
        ("B", "https://b.example.com/", 0.5),
        ("C", "https://c.example.com/", 0.9),
    ])
    run = {
        "scored": scored,
        "selected": [scored[0], scored[2]],   # 只取 C 与同分块里的 A
    }
    stats = rm.tie_stats(run)
    assert stats["candidates"] == 3
    assert stats["tied_candidates"] == 2
    assert stats["tied_ratio"] == pytest.approx(2 / 3, abs=1e-4)
    assert stats["max_block"] == 2
    # A 与 B 同分，一个进了名单一个没进 → 同分块跨过名额边界
    assert stats["blocks_crossing_cutoff"] == 1


def test_tie_expected_metrics_requires_selection_metadata():
    """没有 selection 口径就不能复算——不判，不能拿默认值假装能算。"""
    golden = rm.GoldenSet([{"name": "A", "hosts": ["a.example.com"]}])
    runs = [{"scored": _scored([("A", "https://a.example.com/", 0.5)]), "selected": []}]
    out = rm.tie_expected_metrics(runs, golden, seeds=5)
    assert out["computable"] is False
    assert "selection" in out["reason"]


def test_tie_expected_metrics_is_reproducible_and_bracketed():
    """期望值可复现；且落在"最坏/最好"两种次序之间。"""
    golden = rm.GoldenSet([{"name": "A", "hosts": ["a.example.com"]}])
    scored = _scored([
        ("A", "https://a.example.com/", 0.5),
        ("Z", "https://z.example.com/", 0.5),
    ])
    runs = [{
        "scored": scored,
        "selected": [scored[0]],
        "selection": {"max_count": 1, "min_admission_score": 0.5, "must_include": []},
    }]
    first = rm.tie_expected_metrics(runs, golden, seeds=30)
    second = rm.tie_expected_metrics(runs, golden, seeds=30)
    assert first["computable"] is True
    assert first["keep_l3"]["mean"] == second["keep_l3"]["mean"]
    # 两个候选同分、只取 1 个 → 命中期望落在 0 与 1 之间（不是 0 也不是 1）
    assert 0.0 < first["keep_l3"]["mean"] < 1.0
    assert first["keep_l3"]["sd"] > 0
