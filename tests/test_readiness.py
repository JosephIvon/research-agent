"""交付就绪评估（delivery readiness）纯函数单测。

覆盖四态裁决（blocked/ready/continue/degraded）与维度级状态
（completed/missing/unconfirmed）。纯函数，不走工作流——接线行为由
test_gap_refetch_flow.py 与评测 case 覆盖。
"""
from src.agent.citations import EvidenceStore, make_evidence_id
from src.agent.readiness import (
    DIM_COMPLETED,
    DIM_MISSING,
    DIM_UNCONFIRMED,
    VERDICT_BLOCKED,
    VERDICT_CONTINUE,
    VERDICT_DEGRADED,
    VERDICT_READY,
    assess_delivery_readiness,
    assess_dimension_gaps,
)

_URL = "https://acme.test/"

FULL = [
    {"key": "pricing", "value": "专业版 79 元/月"},
    {"key": "features", "value": "日志检索"},
    {"key": "models", "value": "SaaS 多租户"},
]


def _store(items):
    return EvidenceStore.from_extracted_data([
        {**it, "source_url": _URL} for it in items
    ])


def _ungrounded_violation(store, key):
    ev = next(e for e in store.evidences if e.key == key)
    return {
        "type": "ungrounded_claim",
        "missing_numbers": ["99"],
        "cited_ids": [ev.evidence_id],
    }


# --------------------------------------------------------------------------
# 维度级状态
# --------------------------------------------------------------------------

def test_dimension_status_completed_missing_unconfirmed():
    store = _store(FULL)
    status = assess_dimension_gaps(store)
    assert status["pricing"] == DIM_COMPLETED
    assert status["features"] == DIM_COMPLETED
    assert status["models"] == DIM_COMPLETED
    # 没有证据的维度 → missing
    assert status["integrations"] == DIM_MISSING


def test_dimension_becomes_unconfirmed_when_violation_points_at_it():
    store = _store([FULL[0]])
    status = assess_dimension_gaps(store, [_ungrounded_violation(store, "pricing")])
    assert status["pricing"] == DIM_UNCONFIRMED
    # 无违规时同维度是 completed
    assert assess_dimension_gaps(store)["pricing"] == DIM_COMPLETED


def test_unknown_citation_does_not_mark_any_dimension_unconfirmed():
    """幻觉 id 不参与维度归属（补页面补不出幻觉 id，重生成会删）。"""
    store = _store([FULL[0]])
    status = assess_dimension_gaps(store, [
        {"type": "unknown_citation", "detail": "引用不存在的 id"},
    ])
    assert status["pricing"] == DIM_COMPLETED


# --------------------------------------------------------------------------
# 四态裁决
# --------------------------------------------------------------------------

def _readiness(store, **overrides):
    kwargs = dict(
        evidence_store=store,
        citation_passed=True,
        violations=[],
        source_coverage=1.0,
        evidence_count=len(store.evidences),
        competitor_count=2,
        verification_passed=True,
        quality_usable=True,
    )
    kwargs.update(overrides)
    return assess_delivery_readiness(**kwargs)


def test_verdict_ready_when_passed_and_no_critical_gap():
    r = _readiness(_store(FULL))
    assert r.verdict == VERDICT_READY
    assert r.critical_gaps == []


def test_verdict_blocked_on_unknown_citation():
    store = _store(FULL)
    r = _readiness(store, violations=[{"type": "unknown_citation", "detail": "幻觉"}])
    assert r.verdict == VERDICT_BLOCKED
    assert any("幻觉引用" in g for g in r.hard_gates)


def test_verdict_blocked_on_low_source_coverage():
    r = _readiness(_store(FULL), source_coverage=0.5)
    assert r.verdict == VERDICT_BLOCKED
    assert any("来源覆盖率" in g for g in r.hard_gates)


def test_verdict_continue_when_unpassed_and_refetchable():
    store = _store(FULL)
    r = _readiness(
        store,
        citation_passed=False,
        violations=[_ungrounded_violation(store, "pricing")],
        refetch_eligible=True,
    )
    assert r.verdict == VERDICT_CONTINUE


def test_verdict_degraded_when_unpassed_and_no_refetchable():
    store = _store(FULL)
    r = _readiness(
        store,
        citation_passed=False,
        violations=[_ungrounded_violation(store, "pricing")],
        refetch_eligible=False,
    )
    assert r.verdict == VERDICT_DEGRADED


def test_verdict_degraded_when_critical_dimension_missing():
    """报告自洽 ≠ 研究完整：核心维度缺证据 → degraded，尽管引用通过。"""
    store = _store(FULL[1:] + [
        {"key": "target_users", "value": "中大型企业"},
    ])  # 3 条证据但无 pricing
    r = _readiness(store)
    assert r.verdict == VERDICT_DEGRADED
    assert "pricing" in r.critical_gaps
    assert r.dimensions["pricing"] == DIM_MISSING


def test_verdict_degraded_when_single_competitor():
    """单竞品：可交付但按降级处理（比较结论可信度受限），不是 blocked。"""
    r = _readiness(_store(FULL), competitor_count=1)
    assert r.verdict == VERDICT_DEGRADED
    assert any("样本不足" in reason for reason in r.reasons)
