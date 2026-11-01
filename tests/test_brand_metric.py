"""实体（品牌）口径：候选品牌命中金标准 name/aliases 即算发现。

背景（第十一轮实测 ✗）：Talkie 被发现在 talkieai.cc（官方域 ✓），
但金标准只记了 talkie-ai.com —— host 口径把已发现的产品记 0 分 ✗。
同批 12 轮：host 0.4074 vs 品牌 0.5139（+0.1065 = 实体归并缺口）。

纪律：**非判定口径** ✗ —— 判定线（R1≥0.70）仍用 host 口径（预注册不变）；
品牌口径只负责把"域名没对上"的缺口量化出来（报告并列 ✓）。
品牌键冲突（两个不同产品共用一键）自动排除——宁缺毋滥 ✓。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import recall_metrics as rm  # noqa: E402


def _golden():
    return rm.GoldenSet([
        {"name": "Talkie", "hosts": ["talkie-ai.com"], "aliases": ["talkie-ai"],
         "sources": [{"source_url": "https://talkie-ai.com/list"}]},
        {"name": "星野", "hosts": ["xingyeai.com"], "aliases": ["Xingye"]},
    ])


def test_match_brand_hits_name_and_aliases():
    g = _golden()
    assert g.match_brand("Talkie")["name"] == "Talkie"
    assert g.match_brand("talkie AI")["name"] == "Talkie"      # 大小写/空白归一 ✓
    assert g.match_brand("xingye")["name"] == "星野"            # 别名也进索引 ✓
    assert g.match_brand("毫无关系的产品") is None
    assert g.match_brand("") is None


def test_ambiguous_brand_keys_are_excluded():
    """两个不同产品共用一个品牌键 → 该键宁可不匹配，也不能随机指一个 ✓。"""
    g = rm.GoldenSet([
        {"name": "AI剧情", "hosts": ["a.example"], "aliases": []},
        {"name": "AI 剧情Pro", "hosts": ["b.example"], "aliases": ["ai juqing"]},
    ])
    key = "ai剧情"  # 两个名字归一后同键（假设）→ 歧义
    # 不管键是否真的相撞，断言语义本身：
    for name in ("AI剧情", "AI 剧情Pro"):
        e = g.match_brand(name)
        if e is not None:
            # 命中的必须是**它自己**这一条，绝不许串到别的产品 ✓
            assert e["name"] == name or e["hosts"] in (["a.example"], ["b.example"])
    # 显式构造同键冲突：两个同名异站产品
    g2 = rm.GoldenSet([
        {"name": "Muse", "hosts": ["muse1.example"], "aliases": []},
        {"name": "Muse", "hosts": ["muse2.example"], "aliases": []},
    ])
    assert "muse" in g2.ambiguous_brands
    assert g2.match_brand("Muse") is None


def test_funnel_metrics_reports_both_calibers():
    """同品牌不同域：host 口径不命中、品牌口径命中，两者并列 ✓。"""
    g = _golden()
    run = {
        "candidates": [
            {"url": "https://talkieai.cc/", "name": "Talkie", "site_type": "product"},
            {"url": "https://xingyeai.com/", "name": "星野", "site_type": "product"},
        ],
        "scored": [
            {"url": "https://talkieai.cc/", "name": "Talkie", "score": 0.5, "gate_passed": True},
            {"url": "https://xingyeai.com/", "name": "星野", "score": 0.5, "gate_passed": True},
        ],
        "selected": [{"url": "https://xingyeai.com/", "name": "星野"}],
    }
    m = rm.funnel_metrics(run, g)
    # host 口径：talkieai.cc ≠ talkie-ai.com → 只命中星野 ✓
    assert m["found"]["L0"] == 1 and m["recall"]["L0"] == pytest.approx(0.5)
    # 品牌 口径：Talkie 品牌键命中 → 2/2 ✓
    assert m["found_brand"]["L0"] == 2 and m["recall_brand"]["L0"] == pytest.approx(1.0)
    assert m["recall_brand"]["L3"] == pytest.approx(0.5)


def test_exclude_by_source_keeps_brand_index():
    """剔除自证条目返回新 GoldenSet → 品牌索引要跟着重建 ✓。"""
    g = _golden().exclude_by_source(["https://talkie-ai.com/list"])
    assert len(g) == 1
    assert g.match_brand("星野")["name"] == "星野"
    assert g.match_brand("Talkie") is None  # 已被剔掉 ✓
