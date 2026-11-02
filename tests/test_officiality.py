"""官方性检查：App Store sellerUrl 交叉验证（verified/suspect/unknown）。

背景（2026-11-02 行核 ✗）：talkieai.cc 排"talkie 官网"第一但无开发者印记——
蹭名站。权威归属源 = 应用商店元数据（sellerUrl）：Talkie Lab（SUBSUP）的
sellerUrl 是 talkie-ai.com ⇒ talkieai.cc 是 suspect ✗。

纪律：suspect 名额降权（0.05 → 跌破 0.5 地板 ⇒ 不占名额），L0/L1/L2 池不受
影响 ✓；unknown 永不罚 ✓；查询失败按 unknown（不作废整轮 ✓）；品牌口径
不给 suspect 记命中 ✓。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import officiality as off  # noqa: E402
from src.tools import recall_metrics as rm  # noqa: E402
from src.workflow import search_stages as ss  # noqa: E402
from src.workflow.research_plan import (  # noqa: E402
    ResearchPlan, SearchStage, admit_competitors,
)


def _itunes_rows():
    """iTunes Search API 的返回形态（含同品牌/异品牌/linktree 三种）。"""
    return [
        {"trackName": "Talkie Lab - AI Playground",
         "sellerUrl": "https://talkie-ai.com/"},          # 同品牌 + 品牌域 ✓
        {"trackName": "TalkAI练口语",
         "sellerUrl": "https://ttalkai.com"},             # 异品牌 ✗ 过滤
        {"trackName": "Talkie: AI Companion",
         "sellerUrl": "https://linktr.ee/talkie"},        # 同品牌但 linktree ✗ 过滤
        {"trackName": "Talkie: Your AI Companion",
         "sellerUrl": None},                              # 无 sellerUrl ✗ 过滤
        {"trackName": "Character AI", "sellerUrl": "https://character.ai/"},  # 异品牌 ✗
    ]


@pytest.fixture(autouse=True)
def _fresh_cache():
    off._SELLER_CACHE.clear()


def test_seller_hosts_only_same_brand_with_brand_domain():
    sellers = off.seller_hosts_for_brand("Talkie", fetch=lambda b: _itunes_rows())
    assert sellers == ["talkie-ai.com"]


def test_classify_verified_suspect_unknown():
    assert off.classify_officiality(
        "https://talkie-ai.com/", "Talkie", fetch=lambda b: _itunes_rows()) == "verified"
    # 子域关系也算同站（studio.hangjam.ai ↔ hangjam.ai 的行核教训 ✓）
    assert off.classify_officiality(
        "https://app.talkie-ai.com/login", "Talkie",
        fetch=lambda b: _itunes_rows()) == "verified"
    assert off.classify_officiality(
        "https://talkieai.cc/", "Talkie", fetch=lambda b: _itunes_rows()) == "suspect"
    # 查不到同品牌 App → unknown（永不罚 ✓）
    assert off.classify_officiality(
        "https://whatever.example/", "毫无关系的品牌", fetch=lambda b: []) == "unknown"
    # 网络失败 → unknown ✓
    def _boom(b):
        raise RuntimeError("api down")
    off._SELLER_CACHE.clear()
    assert off.classify_officiality(
        "https://talkieai.cc/", "Talkie", fetch=_boom) == "unknown"


def test_officiality_cache_prevents_refetch():
    calls = []

    def _counting(b):
        calls.append(b)
        return _itunes_rows()

    off.seller_hosts_for_brand("Talkie", fetch=_counting)
    off.seller_hosts_for_brand("Talkie", fetch=_counting)
    off.classify_officiality("https://talkieai.cc/", "Talkie", fetch=_counting)
    assert len(calls) == 1, "进程级缓存：同品牌只查一次 ✓"


def _plan():
    plan = ResearchPlan()
    plan.entity_name = "猫箱"
    plan.criteria = {"admission_rule": "必须有官网", "min_count": 1, "max_count": 1}
    plan.search_plan = [
        SearchStage(stage="entity_anchor", queries=["猫箱 官网"]),
        SearchStage(stage="competitor_discovery", queries=["猫箱 竞品"]),
    ]
    plan.track_keywords = ["AI角色扮演"]
    return plan


def test_suspect_loses_slot_to_verified_peer():
    """同分同域形态下，suspect 不占名额，名额让给 verified 的产品 ✓。"""
    candidates = [
        {"name": "Talkie", "url": "https://talkieai.cc/", "site_type": "product_site",
         "officiality": "suspect"},
        {"name": "星野", "url": "https://xingyeai.com/", "site_type": "product_site",
         "officiality": "verified"},
    ]
    out = admit_competitors(candidates, _plan())
    names = [s["name"] for s in out["selected"]]
    assert names == ["星野"], "suspect 不占稀缺名额 ✓"
    scored_by_name = {s["name"]: s for s in out["scored"]}
    assert scored_by_name["Talkie"]["score"] == pytest.approx(0.45)
    assert scored_by_name["Talkie"]["gate_passed"] is True, "仍如实进池/过闸 ✓"


def test_unknown_is_never_penalized():
    candidates = [
        {"name": "甲", "url": "https://a.example/", "site_type": "product_site",
         "officiality": "unknown"},
        {"name": "乙", "url": "https://b.example/", "site_type": "product_site",
         "officiality": "unknown"},
    ]
    out = admit_competitors(candidates, _plan())
    assert len(out["selected"]) == 1
    assert out["selected"][0]["score"] == pytest.approx(0.5), "unknown 不罚 ✓"


def test_funnel_brand_caliber_skips_suspect_but_host_does_not():
    golden = rm.GoldenSet([
        {"name": "Talkie", "hosts": ["talkie-ai.com"], "aliases": []}])
    run = {
        "candidates": [
            {"url": "https://talkieai.cc/", "name": "Talkie",
             "site_type": "product_site", "aggregator": False,
             "officiality": "suspect"},
        ],
        "scored": [
            {"url": "https://talkieai.cc/", "name": "Talkie", "score": 0.45,
             "gate_passed": True, "officiality": "suspect"},
        ],
        "selected": [],
    }
    m = rm.funnel_metrics(run, golden)
    assert m["recall"]["L0"] == 0.0, "host 口径不受 officiality 影响 ✓"
    assert m["recall_brand"]["L0"] == 0.0, "suspect 不给品牌命中（假阳性剔除 ✓）"


def test_search_stage_annotates_officiality(monkeypatch):
    off._SELLER_CACHE.clear()
    off._SELLER_CACHE["talkie"] = ["talkie-ai.com"]

    class _Fake:
        def search(self, query, num_results=None):
            if query.startswith("猫箱"):
                return [{"url": "https://talkieai.cc/", "title": "Talkie",
                         "content": "x"}]
            return []

    runner = ss.SearchStageRunner(_Fake())
    result = runner.run_competitor_discovery(_plan())
    match = [c for c in result.candidates if c.url == "https://talkieai.cc/"]
    assert match and match[0].officiality == "suspect", "池里候选要带官方性标注 ✓"
    assert result.to_dict()["candidates"][0]["officiality"] == "suspect"
    assert any("官方性检查" in n for n in result.notes)
