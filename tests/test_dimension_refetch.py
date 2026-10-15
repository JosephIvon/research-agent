"""维度 → 候选页面映射（dimension_refetch）纯函数单测。

覆盖：意图 tiers 注入 discover 的选页效果、per-site 维度状态、决策与预算。
接线行为由 test_gap_refetch_flow.py 与评测 case 覆盖。
"""
from src.agent.citations import EvidenceStore
from src.agent.dimension_refetch import (
    DIMENSION_PAGE_TIERS,
    _merge_tiers,
    assess_site_dimension_gaps,
    plan_dimension_refetch,
)
from src.agent.readiness import (
    DIM_COMPLETED,
    DIM_MISSING,
    DIM_UNCONFIRMED,
    VERDICT_DEGRADED,
    assess_delivery_readiness,
)
from src.crawler.web_crawler import WebCrawler

_URL_A = "https://acme.test/"
_URL_B = "https://beta.test/"

_SITES = [
    {"key": "acme", "url": _URL_A},
    {"key": "beta", "url": _URL_B},
]


def _store(items):
    return EvidenceStore.from_extracted_data([
        {**it, "source_url": it.get("source_url", _URL_A)} for it in items
    ])


# --------------------------------------------------------------------------
# 意图 tiers：注入 discover 的选页效果
# --------------------------------------------------------------------------

def test_pricing_tiers_prioritize_pricing_page_in_discover(monkeypatch):
    monkeypatch.setattr(
        "src.crawler.web_crawler.validate_fetch_url", lambda u: u,
    )
    html = (
        '<a href="/pricing">价格与套餐</a>'
        '<a href="/blog">博客</a>'
        '<a href="/about">关于我们</a>'
    )
    links = WebCrawler.discover_research_links(
        _URL_A, html, max_links=4, priority_tiers=DIMENSION_PAGE_TIERS["pricing"],
    )
    assert any(u.endswith("/pricing") for u in links)
    pricing_i = next(i for i, u in enumerate(links) if u.endswith("/pricing"))
    blog_i = next(i for i, u in enumerate(links) if u.endswith("/blog"))
    assert pricing_i < blog_i


def test_pricing_tiers_outrank_default_tiers_for_pricing_page(monkeypatch):
    """同样的 /pricing 链接，用定价意图 tiers 应排到默认先验之前。"""
    monkeypatch.setattr(
        "src.crawler.web_crawler.validate_fetch_url", lambda u: u,
    )
    html = (
        '<a href="/pricing">定价</a>'
        '<a href="/features">产品功能</a>'
        '<a href="/blog">博客</a>'
    )
    default = WebCrawler.discover_research_links(_URL_A, html, max_links=4)
    pricing = WebCrawler.discover_research_links(
        _URL_A, html, max_links=4, priority_tiers=DIMENSION_PAGE_TIERS["pricing"],
    )
    assert default[0].endswith("/pricing")  # 默认先验也认定价页
    assert pricing[0].endswith("/pricing")


def test_merge_tiers_dedups_across_dimensions():
    merged = _merge_tiers(["pricing", "models"])
    assert len(merged) == 2  # (10, pricing 词) + (5, pricing 词) + (10, models 词) + (5, models 词)
    weights = [w for w, _ in merged]
    assert weights == sorted(weights, reverse=True)
    terms_10 = set().union(*[t for w, t in merged if w == 10])
    assert "pricing" in terms_10 and "模型" in terms_10


# --------------------------------------------------------------------------
# per-site 维度状态
# --------------------------------------------------------------------------

def test_site_dimension_gaps_are_per_host():
    store = _store([
        {"key": "pricing", "value": "专业版 79 元/月", "source_url": _URL_A},
        {"key": "features", "value": "日志检索", "source_url": _URL_A},
        {"key": "features", "value": "审计日志", "source_url": _URL_B},  # B 有功能没定价
    ])
    status = assess_site_dimension_gaps(store, _SITES)
    assert status["acme"]["pricing"] == DIM_COMPLETED
    assert status["beta"]["pricing"] == DIM_MISSING
    assert status["beta"]["features"] == DIM_COMPLETED


def test_site_dimension_gaps_key_falls_back_to_host():
    """API 直达的 target_sites 可能没有 key，回退 host。"""
    sites = [{"url": _URL_A}]
    store = _store([{"key": "pricing", "value": "79 元/月", "source_url": _URL_A}])
    status = assess_site_dimension_gaps(store, sites)
    assert status["acme.test"]["pricing"] == DIM_COMPLETED


# --------------------------------------------------------------------------
# plan_dimension_refetch 决策
# --------------------------------------------------------------------------

def test_plan_eligible_when_core_dimension_missing_on_a_site():
    store = _store([
        {"key": "pricing", "value": "专业版 79 元/月", "source_url": _URL_A},
        {"key": "features", "value": "日志检索", "source_url": _URL_A},
        {"key": "features", "value": "审计日志", "source_url": _URL_B},
    ])
    decision = plan_dimension_refetch(store, _SITES)
    assert decision.eligible is True
    assert [c.site_key for c in decision.candidates] == ["beta"]
    cand = decision.candidates[0]
    assert cand.dims == ("pricing",)
    # 2 = 首页 + 1 个维度意图页；1 会让 fetch_site_profile 只抓首页、选页失效
    assert cand.pages == 2
    assert any("pricing" in t for _, terms in cand.page_tiers for t in terms)


def test_plan_ineligible_when_all_core_dimensions_covered():
    store = _store([
        {"key": "pricing", "value": "79 元/月", "source_url": _URL_A},
        {"key": "features", "value": "日志检索", "source_url": _URL_A},
        {"key": "pricing", "value": "69 元/月", "source_url": _URL_B},
        {"key": "features", "value": "审计日志", "source_url": _URL_B},
    ])
    decision = plan_dimension_refetch(store, _SITES)
    assert decision.eligible is False
    assert decision.reason == "no_core_dimension_gap"


def test_plan_budget_caps_candidate_sites():
    sites = [
        {"key": f"s{i}", "url": f"https://s{i}.test/"} for i in range(5)
    ]
    store = EvidenceStore()  # 全部站点都缺核心维度
    decision = plan_dimension_refetch(store, sites, max_pages=3)
    assert decision.eligible is True
    assert len(decision.candidates) == 3  # 预算刹车


def test_plan_no_sites():
    decision = plan_dimension_refetch(EvidenceStore(), [])
    assert decision.eligible is False


def test_plan_sees_unconfirmed_as_completed_and_does_not_refetch():
    """unconfirmed 维度在维度补抓里不可见（视为 completed）——职责分工：
    readiness 暴露它（degraded），维度补抓不补它（引用错归数值补抓/重生成）。

    钉住现状语义：`assess_site_dimension_gaps` 刻意不接收 violations，
    防止将来误加后对"引用错"重复补抓（且 ambiguous 时站点归属不清）。
    """
    store = _store([
        {"key": "pricing", "value": "专业版 79 元/月", "source_url": _URL_A},
        {"key": "features", "value": "日志检索", "source_url": _URL_A},
        {"key": "pricing", "value": "69 元/月", "source_url": _URL_B},
        {"key": "features", "value": "审计日志", "source_url": _URL_B},
    ])
    fev = next(e for e in store.evidences if e.key == "features" and e.url.startswith(_URL_A))
    violations = [{
        "type": "ungrounded_claim",
        "missing_numbers": ["99"],
        "cited_ids": [fev.evidence_id],
    }]

    # 维度补抓：看不到违规 → 视为 completed → 不触发
    decision = plan_dimension_refetch(store, _SITES)
    assert decision.eligible is False
    assert decision.reason == "no_core_dimension_gap"

    # readiness：看得到违规 → features unconfirmed → 核心缺口 → degraded
    r = assess_delivery_readiness(
        evidence_store=store,
        citation_passed=True,  # 引用自洽 ≠ 研究完整：无违规判定但维度被违规指向
        violations=violations,
        evidence_count=len(store.evidences),
    )
    assert r.dimensions["features"] == DIM_UNCONFIRMED
    assert r.verdict == VERDICT_DEGRADED
    assert "features" in r.critical_gaps
