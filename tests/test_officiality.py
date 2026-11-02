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
    off._FETCH_FAILED.clear()
    off._REDIRECT_CACHE.clear()
    off.set_disk_cache(None)


def _boom(b, c="us"):
    raise RuntimeError("api down")


def _disk_fallback_itunes_rows():
    return _itunes_rows()


def test_seller_hosts_only_same_brand_with_brand_domain():
    sellers = off.seller_hosts_for_brand("Talkie", fetch=lambda b, c="us": _itunes_rows())
    assert sellers == ["talkie-ai.com"]


def test_classify_verified_suspect_unknown():
    assert off.classify_officiality(
        "https://talkie-ai.com/", "Talkie", fetch=lambda b, c="us": _itunes_rows()) == "verified"
    # 子域关系也算同站（studio.hangjam.ai ↔ hangjam.ai 的行核教训 ✓）
    assert off.classify_officiality(
        "https://app.talkie-ai.com/login", "Talkie",
        fetch=lambda b, c="us": _itunes_rows()) == "verified"
    off._REDIRECT_CACHE["talkieai.cc"] = None  # 短路 ✓
    assert off.classify_officiality(
        "https://talkieai.cc/", "Talkie", fetch=lambda b, c="us": _itunes_rows(),
        redirect_lookup=lambda h: None) == "suspect"
    # 查不到同品牌 App → unknown（永不罚 ✓）
    assert off.classify_officiality(
        "https://whatever.example/", "毫无关系的品牌", fetch=lambda b, c="us": []) == "unknown"
    # 网络失败 → unknown ✓
    def _boom(b, c="us"):
        raise RuntimeError("api down")
    off._SELLER_CACHE.clear()
    assert off.classify_officiality(
        "https://talkieai.cc/", "Talkie", fetch=_boom) == "unknown"


def test_officiality_cache_prevents_refetch():
    calls = []

    def _counting(b, c="us"):
        calls.append(b)
        return _itunes_rows()

    off._REDIRECT_CACHE["talkieai.cc"] = None  # 短路：不真探重定向 ✓
    off.seller_hosts_for_brand("Talkie", fetch=_counting)
    off.seller_hosts_for_brand("Talkie", fetch=_counting)
    off.classify_officiality("https://talkieai.cc/", "Talkie", fetch=_counting,
                             redirect_lookup=lambda h: None)
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


def test_redirect_exemption_saves_old_official_domain():
    """行核误伤案例回归 ✗：replika.ai（旧官方域）200 跳到 replika.com
    （= sellerUrl）——重定向豁免要把它判成 verified ✓。"""
    off._REDIRECT_CACHE.clear()
    assert off.classify_officiality(
        "https://replika.ai/", "Replika",
        fetch=lambda b, c="us": [{"trackName": "Replika: My AI Friend",
                          "sellerUrl": "https://replika.com/"}],
        redirect_lookup=lambda h: "replika.com") == "verified"
    # 不落开发者站的跳转 → 仍 suspect ✓
    off._REDIRECT_CACHE.clear()
    assert off.classify_officiality(
        "https://talkieai.cc/", "Talkie",
        fetch=lambda b, c="us": [{"trackName": "Talkie Lab - AI Playground",
                          "sellerUrl": "https://talkie-ai.com/"}],
        redirect_lookup=lambda h: "somewhere-else.example") == "suspect"
    off._REDIRECT_CACHE.clear()


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


def test_verification_skips_suspect_but_keeps_counter_evidence():
    """逐家验证：suspect 的站内验证查询跳过（给蹭名站找"定价/功能"是毒数据 ✗），
    反证照常（第三方评价与域主无关 ✓）；unknown 永不跳过 ✓。"""
    class _Recording:
        def __init__(self):
            self.queries = []
        def search(self, query, num_results=None):
            self.queries.append(query)
            return [{"url": f"https://r{len(self.queries)}.test/",
                     "title": "x", "content": "y"}]

    stub = _Recording()
    runner = ss.SearchStageRunner(stub)
    pages = runner.run_verification(
        _plan(), ["Talkie", "星野"],
        officiality_by_name={"Talkie": "suspect", "星野": "verified"})
    by = {}
    for p in pages:
        by.setdefault((p["competitor"], p["stage"]), 0)
        by[(p["competitor"], p["stage"])] += 1
    assert ("Talkie", "per_competitor_verify") not in by, "suspect 跳过逐家验证 ✓"
    assert ("Talkie", "counter_evidence") in by, "suspect 反证照常 ✓"
    assert ("星野", "per_competitor_verify") in by, "verified 照常 ✓"

    stub2 = _Recording()
    runner2 = ss.SearchStageRunner(stub2)
    pages2 = runner2.run_verification(
        _plan(), ["乙"], officiality_by_name={"乙": "unknown"})
    assert {p["stage"] for p in pages2} == {"per_competitor_verify",
                                            "counter_evidence"}, "unknown 永不罚 ✓"


def test_disk_cache_roundtrip_and_failure_exclusion(tmp_path):
    """落盘缓存（§15）：① 成功判定写盘、跨"进程"（清空进程缓存）复用 ✓；
    ② iTunes 失败的品牌**不落盘** ✗（间歇失败不能固化成 unknown ✓）；
    ③ 失败时盘里有历史判定 → 兜底生效 ✓。"""
    import json as _json

    cache_file = tmp_path / "officiality_cache.json"

    # 第一"进程"：talkie 查询成功，broken 查询失败
    off._SELLER_CACHE.clear(); off._FETCH_FAILED.clear(); off._REDIRECT_CACHE.clear()
    def _fetch(brand, country="us"):
        if brand == "broken":
            raise RuntimeError("api down")
        if brand == "talkie":
            return [{"trackName": "Talkie Lab - AI Playground",
                     "sellerUrl": "https://talkie-ai.com/"}]
        return []
    off._REDIRECT_CACHE["talkieai.cc"] = None  # 短路 ✓
    off.annotate_officiality(
        [{"name": "Talkie", "url": "https://talkieai.cc/", "officiality": ""},
         {"name": "Broken", "url": "https://b.example/", "officiality": ""}],
        fetch=_fetch, cache_path=str(cache_file))
    disk = _json.loads(cache_file.read_text(encoding="utf-8"))
    assert disk["brands"].get("talkie") == ["talkie-ai.com"], "成功判定落盘 ✓"
    assert "broken" not in disk["brands"], "失败品牌不落盘 ✗ 不能固化成 unknown ✓"

    # 第二"进程"：进程缓存清空、启动时载盘、iTunes 全挂 → talkie 从盘兜底 ✓
    off._SELLER_CACHE.clear(); off._FETCH_FAILED.clear(); off._REDIRECT_CACHE.clear()
    off.set_disk_cache(str(cache_file))
    verdict = off.classify_officiality(
        "https://talkieai.cc/", "Talkie", fetch=_boom, redirect_lookup=lambda h: None)
    assert verdict == "suspect", "盘缓存兜底：iTunes 挂了也能判 suspect ✓"



def test_cn_fallback_bounded_and_short_circuited():
    """cn 区回退（§16）：us 查不到开发者站才查 cn（CN 产品补齐 ✓，最多 2 查 ✓）；
    us 已有开发者站就不查 cn ✓；us 失败按失败处理（不落盘 ✓）。"""
    calls = []

    def _us_empty(brand, country="us"):
        calls.append(country)
        if country == "cn":
            return [{"trackName": "星野", "sellerUrl": "https://xingyeai.com/"}]
        return []

    off._SELLER_CACHE.clear(); off._FETCH_FAILED.clear()
    assert off.seller_hosts_for_brand("星野", fetch=_us_empty) == ["xingyeai.com"], "cn 回退补齐 ✓"
    assert calls == ["us", "cn"], "us 空才查 cn（有界 ✓）"

    # CJK 品牌的 linktree 要被黑名单挡住 ✗（域含品牌词检查对 CJK 永假）
    calls.clear()
    def _cn_linktree(brand, country="us"):
        calls.append(country)
        if country == "cn":
            return [{"trackName": "星野", "sellerUrl": "https://linktr.ee/xingye"}]
        return []

    off._SELLER_CACHE.clear(); off._FETCH_FAILED.clear()
    assert off.seller_hosts_for_brand("星野", fetch=_cn_linktree) == [], "CJK linktree 不算开发者站 ✓"
    assert calls == ["us", "cn"], "黑名单拒收后 us 已查 cn 也已查 ✓"

    calls.clear()
    def _us_hit(brand, country="us"):
        calls.append(country)
        return [{"trackName": "Talkie Lab - AI Playground",
                 "sellerUrl": "https://talkie-ai.com/"}]

    off._SELLER_CACHE.clear(); off._FETCH_FAILED.clear()
    assert off.seller_hosts_for_brand("Talkie", fetch=_us_hit) == ["talkie-ai.com"]
    assert calls == ["us"], "us 有开发者站不查 cn ✓"

    calls.clear()
    off._SELLER_CACHE.clear(); off._FETCH_FAILED.clear()

    def _boom_calling(brand, country="us"):
        calls.append(country)
        _boom(brand, country)

    assert off.seller_hosts_for_brand("甲乙丙", fetch=_boom_calling) == []
    assert off._FETCH_FAILED, "两区都失败按失败处理（不落盘 ✓）"
    assert calls == ["us", "cn"], "us 失败也要试 cn（限流是整层抖动，us 挂 ≠ cn 挂 ✓）"

def test_matrix_row_warning_for_suspect_provenance():
    """矩阵 provenance（§14 产品侧）：suspect 行带 ⚠️ 官方性警示（数据来源可疑），
    verified/unknown 行不加注（不刷屏 ✓）；按主机对齐（抓取页与入选页不同路径 ✓）。"""
    from src.workflow.competitor_matrix import build_matrix

    comp = {"url": "https://talkieai.cc/pricing", "name": "Talkie",
            "officiality": "suspect",
            "extracted_data": [{"feature": "x", "source_url": "https://talkieai.cc/pricing"}]}
    matrix = build_matrix("测试", competitors=[comp])
    assert any("官方性存疑" in w for w in matrix.rows[0].warnings), "suspect 行带警示 ✓"
    # verified 行不加注
    comp2 = dict(comp, url="https://replika.com/", officiality="verified")
    m2 = build_matrix("测试", competitors=[comp2])
    assert not any("官方性存疑" in w for w in m2.rows[0].warnings)
    # 参数映射兜底（行上没挂字段时按主机查 ✓）
    comp3 = {"url": "https://talkieai.cc/", "name": "Talkie", "extracted_data": []}
    m3 = build_matrix("测试", competitors=[comp3],
                      officiality_by_host={"talkieai.cc": "suspect"})
    assert any("官方性存疑" in w for w in m3.rows[0].warnings)


def test_search_stage_annotates_officiality(monkeypatch):
    off._SELLER_CACHE.clear()
    off._SELLER_CACHE["talkie"] = ["talkie-ai.com"]
    # hermetic（第十八轮教训 ✗ 66s 真网络）：未播种品牌不再真打 iTunes，
    # 重定向探测也用进程缓存短路 ✓
    monkeypatch.setattr(off, "_itunes_search", lambda brand, country="us": [])
    off._REDIRECT_CACHE["talkieai.cc"] = None

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
