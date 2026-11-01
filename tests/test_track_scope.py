"""赛道名册 / 同赛道闸门 / 名册与测量集互斥 的测试。

口径纪律（docs/discovery-recall-measurement.md §8）：
名册是**闸门的知识**，留出集是**测量的分母**——两者必须互斥，
否则闸门放行名册产品就会自己把 keep 率涨上去（自证，不是改进）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.workflow.track_scope import (  # noqa: E402
    check_same_track,
    load_registry,
    load_scope,
)

UNIVERSE_FILE = ROOT / "evals" / "discovery_golden" / "track_universe.json"
REGISTRY_FILE = ROOT / "assets" / "track_registry.json"


def _universe() -> dict:
    return json.loads(UNIVERSE_FILE.read_text(encoding="utf-8"))


def test_registry_and_heldout_are_disjoint():
    """名册与留出集必须互斥（否则测量自证）。"""
    universe = _universe()
    registry = {e["name"] for e in _universe()["golden"] if e.get("split") == "registry"}
    heldout = {e["name"] for e in universe["golden"] if e.get("split") == "heldout"}
    assert registry, "名册不能为空"
    assert heldout, "留出集不能为空"
    assert not (registry & heldout), f"名册与留出集重叠：{sorted(registry & heldout)[:5]}"
    assert len(registry) + len(heldout) == len(universe["golden"])


def test_registry_has_no_circular_or_webless_entries():
    """名册里不能有：本管线自己发现的（循环论证）+ 只有商店页的（对网页闸门无用）。"""
    for entry in _universe()["golden"]:
        if entry.get("split") != "registry":
            continue
        assert entry.get("source_tier") not in ("pipeline", "store_only"), entry["name"]
        assert entry.get("hosts"), entry["name"]


def test_registry_asset_matches_the_split():
    """名册资产（生产侧）与金标准里的 registry 标记必须一致。"""
    registry = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    expected = {e["name"] for e in _universe()["golden"] if e.get("split") == "registry"}
    assert {e["name"] for e in registry["entries"]} == expected


def test_known_off_track_sites_are_rejected():
    """实测到的假阳性必须被拦（这是同赛道闸门存在的理由）。"""
    cases = [
        ("Talkie", "https://talkie.ai/", "Talkie.ai | AI Medical Receptionist for Better Patient Access"),
        ("星野", "https://hoshinoresorts.com/zh/", "星野集团 - 度假村与酒店"),
        ("Local On", "https://www.mewatch.sg/", "On-Demand Dramas, Movies, Sports, News & LIVE"),
    ]
    for name, url, desc in cases:
        ok, why = check_same_track(name, url, desc)
        assert not ok, f"{url} 应该被拦，实际放行：{why}"
        assert "跨行业" in why


def test_registry_sites_pass_the_gate():
    """名册里的站点必须放行（闸门不能把知道的真竞品也拦了）。"""
    registry = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    for entry in registry["entries"][:40]:
        ok, why = check_same_track(entry["name"], f"https://{entry['hosts'][0]}/", "")
        assert ok, f"{entry['name']} 被拦了：{why}"


def test_golden_products_are_not_rejected_as_off_track():
    """**金标准里的产品不能被跨行业词误伤**（误伤 = 直接压召回）。"""
    rejected = []
    for entry in _universe()["golden"]:
        hosts = entry.get("hosts") or []
        if not hosts:
            continue
        text = f"{entry['name']} {entry.get('evidence') or ''}"
        ok, why = check_same_track(entry["name"], f"https://{hosts[0]}/", text)
        if not ok:
            rejected.append((entry["name"], why))
    assert not rejected, f"金标准产品被跨行业词误伤：{rejected[:5]}"


def test_unknown_candidate_passes_but_is_marked():
    """没证据的候选放行但记账（宁可漏杀也不误杀）。"""
    ok, why = check_same_track("SomeTool", "https://example.com/", "一个普通的工具")
    assert ok is True
    assert "未判定" in why


def test_scope_asset_has_both_keyword_lists():
    scope = load_scope()
    assert scope.positive_keywords, "赛道词不能为空"
    assert scope.off_track_keywords, "跨行业词不能为空"
    # 跨行业词要是具体的短语，不能是"预约"这种常用词（会误伤）
    for risky in ("预约", "人才", "生活", "工具", "app"):
        assert risky not in scope.off_track_keywords, f"跨行业词过泛：{risky}"


def test_split_is_reproducible():
    """切分必须可复现（固定哈希，不随机）。"""
    sys.path.insert(0, str(ROOT))
    from tool_build_track_split import build, bucket_of

    universe = _universe()
    first = build(json.loads(json.dumps(universe)))
    second = build(json.loads(json.dumps(universe)))
    assert first["counts"] == second["counts"]
    assert bucket_of("星野") == bucket_of("星野")
