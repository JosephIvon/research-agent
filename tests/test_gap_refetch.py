"""缺口定向补抓的判定测试（`plan_gap_refetch`）。

这一层是纯函数：不联网、不调 LLM，所以测试可以只靠构造违规清单和证据库
把判定树的每条边走一遍，不需要任何夹具或网络。

**重点不在「能判出 eligible」，而在「不该 eligible 时有没有忍住」。**
误判成 eligible 的代价是真金白银的抓取与抽取；误判成不 eligible 的代价是
功能静默失效。后者更难发现，所以两类都要钉。
"""
from src.agent.citations import EvidenceStore, verify_report
from src.agent.gap_refetch import (
    REASON_ALREADY_AVAILABLE,
    REASON_AMBIGUOUS_MATCH,
    REASON_AMBIGUOUS_TARGET,
    REASON_ELIGIBLE,
    REASON_NO_ELIGIBLE_VIOLATION,
    REASON_PAGE_BUDGET_ZERO,
    REASON_VIOLATION_NOT_STRUCTURED,
    UNMAPPED_DIMENSION,
    dimension_of,
    plan_gap_refetch,
    site_of,
)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _store(items):
    """items: [(url, key, value), ...] → (store, {key: evidence_id})"""
    store = EvidenceStore.from_extracted_data([
        {"source_url": u, "key": k, "value": v} for u, k, v in items
    ])
    ids = {}
    for ev in store.evidences:
        ids.setdefault(ev.key, ev.evidence_id)
    return store, ids


def _ungrounded(ids, missing_numbers, sentence="某产品专业版售价 99 元/月"):
    return {
        "type": "ungrounded_claim",
        "detail": f"数值 {'、'.join(missing_numbers)} 在所引证据原文中找不到：{sentence}",
        "missing_numbers": list(missing_numbers),
        "cited_ids": list(ids),
        "sentence": sentence,
    }


ACME = "https://acme.com"
GLOBEX = "https://globex.com"


# --------------------------------------------------------------------------
# 违规类型分流：第一版只处理 ungrounded_claim
# --------------------------------------------------------------------------

def test_only_ungrounded_claim_triggers_refetch():
    """另两类违规不触发补抓，理由要说得清是「没有候选违规」而不是「有但被拒」。"""
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch([
        {"type": "unknown_citation", "detail": "引用了不存在的证据 id [e:deadbeef]"},
        {"type": "uncited_claim", "detail": "含数值的结论未标注证据", "kind": "plain"},
    ], store, [{"url": ACME, "name": "Acme"}])

    assert d.eligible is False
    assert d.reason == REASON_NO_ELIGIBLE_VIOLATION
    assert d.candidates == []
    assert d.seen_violation_types == {"unknown_citation": 1, "uncited_claim": 1}


def test_real_gap_is_eligible_and_carries_dimension_and_boost_terms():
    """真缺口：目标站点上没有这条数值，应进入补抓候选并带上维度关键词。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{ACME}/home", "features", "支持 SSO 与审计日志"),
    ])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"])],
        store, [{"url": ACME, "name": "Acme"}],
    )

    assert d.eligible is True, d.reason
    assert d.reason == REASON_ELIGIBLE
    assert len(d.candidates) == 1
    c = d.candidates[0]
    assert c.competitor_url == "acme.com"
    assert c.competitor_name == "Acme"
    assert c.dimension == "pricing"
    assert c.missing_numbers == ["99"]
    # boost 必须含定价类词，否则站内候选页根本排不出来
    assert "定价" in c.boost_terms and "pricing" in c.boost_terms


# --------------------------------------------------------------------------
# already_available：证据已在手，只是引错了 —— 不该补抓
# --------------------------------------------------------------------------

def test_number_present_in_same_dimension_elsewhere_is_already_available():
    """同站同维度另有证据含该数值 → 第三次生成换引用即可，不花钱抓。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{ACME}/plans", "pricing", "专业版 99 元/月含 10 席位"),
    ])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"])],
        store, [{"url": ACME, "name": "Acme"}],
    )

    assert d.eligible is False
    assert d.reason == REASON_ALREADY_AVAILABLE
    assert d.skipped.get(REASON_ALREADY_AVAILABLE) == 1


def test_number_in_other_dimension_is_NOT_already_available():
    """**关键回归守卫**：同站另一个维度里的 99 不是定价证据，仍应补抓。

    把这条判成 already_available 就等于把真缺口永久判成已解决——
    这个数字可能是 API 限额、用户数或版本号。误判会让功能静默失效，
    而且从外部完全看不出来。
    """
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{ACME}/docs", "api_flow", "限流 99 次/分钟"),
    ])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"])],
        store, [{"url": ACME, "name": "Acme"}],
    )

    assert d.eligible is True, (
        f"api_flow 里的 99 不该被当成定价证据，实得 reason={d.reason}"
    )
    assert d.candidates[0].missing_numbers == ["99"]


def test_number_on_another_competitor_is_not_available():
    """别家竞品的同数字与本竞品无关，仍应补抓。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "专业版 99 元/月"),
    ])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"])],
        store, [{"url": ACME, "name": "Acme"}, {"url": GLOBEX, "name": "Globex"}],
    )

    assert d.eligible is True, f"别家的证据不应算数，实得 {d.reason}"
    assert d.candidates[0].competitor_url == "acme.com"


def test_unknown_dimension_evidence_is_treated_as_possibly_related():
    """词表认不出的 key 不该把真缺口误杀——收紧靠额度与注入防护，不靠这一条。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{ACME}/misc", "weird_custom_key", "杂项 99"),
    ])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"])],
        store, [{"url": ACME, "name": "Acme"}],
    )

    assert d.eligible is False
    assert d.reason == REASON_ALREADY_AVAILABLE


# --------------------------------------------------------------------------
# 竞品归属
# --------------------------------------------------------------------------

def test_citation_spanning_two_competitors_is_ambiguous():
    """一句同时引了 A、B 两家的证据 → 归属不清，安全跳过。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "入门版 31 元/月"),
    ])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"], _ids_globex(store)], ["99"])],
        store, [{"url": ACME, "name": "Acme"}, {"url": GLOBEX, "name": "Globex"}],
    )

    assert d.eligible is False
    assert d.reason == REASON_AMBIGUOUS_TARGET
    assert d.skipped.get(REASON_AMBIGUOUS_TARGET) == 1


def _ids_globex(store):
    for ev in store.evidences:
        if ev.url.startswith(GLOBEX):
            return ev.evidence_id
    raise AssertionError("夹具缺 globex 证据")


def test_evidence_without_source_url_is_ambiguous():
    """证据没标来源 → 无从归属，跳过。"""
    store, ids = _store([("", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"])],
        store, [{"url": ACME, "name": "Acme"}],
    )

    assert d.eligible is False
    assert d.reason == REASON_AMBIGUOUS_TARGET


def test_site_outside_target_scope_is_ambiguous():
    """证据来源不在本次任务的目标站点清单内 → 不擅自替它补抓。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "入门版 31 元/月"),
    ])

    d = plan_gap_refetch(
        [_ungrounded([_ids_globex(store)], ["99"])],
        store, [{"url": ACME, "name": "Acme"}],   # 只声明 Acme
    )

    assert d.eligible is False
    assert d.reason == REASON_AMBIGUOUS_TARGET


# --------------------------------------------------------------------------
# 结构化字段缺失：必须显式失败，不能靠解析中文句子猜
# --------------------------------------------------------------------------

def test_missing_structured_fields_is_reported_not_guessed():
    """结构化字段没了就判不可用——绝不能回退去解析中文 detail 猜缺口。"""
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch([
        {"type": "ungrounded_claim",
         "detail": "数值 99 在所引证据原文中找不到：某产品专业版售价 99 元/月"},
    ], store, [{"url": ACME, "name": "Acme"}])

    assert d.eligible is False
    assert d.skipped.get(REASON_VIOLATION_NOT_STRUCTURED) == 1
    assert d.reason == REASON_VIOLATION_NOT_STRUCTURED, (
        "字段缺失的 reason 码必须是它自己，不能被塌缩成别的码——"
        "否则线上分不清是字段回归了还是判定问题"
    )


def test_unresolvable_citation_id_is_ambiguous():
    """违规里的 id 在 store 里查不到 → 不同源，不猜。"""
    store, _ = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch(
        [_ungrounded(["deadbeef1234"], ["99"])],
        store, [{"url": ACME, "name": "Acme"}],
    )

    assert d.eligible is False
    assert d.reason == REASON_AMBIGUOUS_TARGET


# --------------------------------------------------------------------------
# 额度
# --------------------------------------------------------------------------

def test_zero_page_budget_short_circuits():
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])
    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"])],
        store, [{"url": ACME, "name": "Acme"}], max_pages=0,
    )
    assert d.eligible is False
    assert d.reason == REASON_PAGE_BUDGET_ZERO
    assert d.page_budget == 0


def test_budget_capped_by_candidate_count():
    """候选数少于额度时，额度收敛到候选数，不虚报。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "入门版 31 元/月"),
    ])
    acme_id = [ev.evidence_id for ev in store.evidences if ev.url.startswith(ACME)][0]
    globex_id = _ids_globex(store)

    d = plan_gap_refetch(
        [_ungrounded([acme_id], ["99"]), _ungrounded([globex_id], ["88"])],
        store, [{"url": ACME, "name": "Acme"}, {"url": GLOBEX, "name": "Globex"}],
        max_pages=10,
    )

    assert d.eligible is True
    assert len(d.candidates) == 2
    assert d.page_budget == 2, "额度应收敛到候选数"
    assert all(c.per_candidate_pages >= 1 for c in d.candidates)


# --------------------------------------------------------------------------
# 聚合：同站同维度的多个缺口共用一次抓取
# --------------------------------------------------------------------------

def test_same_site_same_dimension_gaps_are_merged():
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"]),
         _ungrounded([ids["pricing"]], ["199"])],
        store, [{"url": ACME, "name": "Acme"}],
    )

    assert d.eligible is True
    assert len(d.candidates) == 1, "同站同维度应聚合成一个候选"
    assert d.candidates[0].missing_numbers == ["99", "199"]


def test_duplicate_missing_numbers_are_deduped_when_merged():
    """两句都缺同一个 99 时，合并后只能出现一次——否则补抓计划会重复要求同一页。"""
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"], sentence="专业版 99 元/月"),
         _ungrounded([ids["pricing"]], ["99"], sentence="年付 99 元/月")],
        store, [{"url": ACME, "name": "Acme"}],
    )

    assert d.eligible is True
    assert len(d.candidates) == 1
    assert d.candidates[0].missing_numbers == ["99"], (
        f"重复的缺失数值未被去重：{d.candidates[0].missing_numbers}"
    )


# --------------------------------------------------------------------------
# 安全：凭据绝不能进决策对象（决策会被 SSE 发出去）
# --------------------------------------------------------------------------

def test_credentials_never_reach_the_decision():
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"])],
        store,
        [{"url": ACME, "name": "Acme",
          "auth_credentials": {"username": "u", "password": "p"},
          "login_url": "https://acme.com/login"}],
    )

    blob = repr(d.to_dict())
    assert "auth_credentials" not in blob
    assert "password" not in blob
    assert "u" == d.candidates[0].competitor_name or d.candidates[0].competitor_name == "Acme"


# --------------------------------------------------------------------------
# 与 verify_report 的接线：结构化字段真的产出了
# --------------------------------------------------------------------------

def test_verify_report_emits_structured_fields_on_ungrounded_claim():
    """端到端：真实报告跑一遍校验，违规必须带机器可读字段。"""
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])
    report = "Acme 专业版售价 99 元/月。[e:%s]" % ids["pricing"]

    result = verify_report(report, store)

    ungrounded = [v for v in result["violations"] if v["type"] == "ungrounded_claim"]
    assert len(ungrounded) == 1, result["violations"]
    v = ungrounded[0]
    assert v["missing_numbers"] == ["99"]
    assert v["cited_ids"] == [ids["pricing"]]
    assert "99" in v["sentence"]
    # 原有 detail 必须保留：重生成提示词还在用它
    assert "在所引证据原文中找不到" in v["detail"]

    # 并且这条违规可以直接喂进判定，无需人工加工
    d = plan_gap_refetch(result["violations"], store, [{"url": ACME, "name": "Acme"}])
    assert d.eligible is True


# --------------------------------------------------------------------------
# 词表本身
# --------------------------------------------------------------------------

def test_dimension_alias_normalisation():
    assert dimension_of("pricing") == "pricing"
    assert dimension_of("Price") == "pricing"          # 大小写不敏感
    assert dimension_of("PRICE") == "pricing"
    assert dimension_of("price") == "pricing"           # 同义写法
    assert dimension_of("providers") == "models"
    assert dimension_of("rate_limit") == "api_flow"
    assert dimension_of("根本没这个词") == UNMAPPED_DIMENSION
    assert dimension_of("") == UNMAPPED_DIMENSION


def test_site_of_groups_pages_of_same_competitor():
    """同一竞品的不同页面必须归到同一个站点标识，否则归属会碎掉。"""
    assert site_of("https://acme.com/pricing") == "acme.com"
    assert site_of("https://ACME.com/pricing") == "acme.com"
    assert site_of("https://acme.com/docs/api") == "acme.com"
    assert site_of("https://acme.com") != site_of("https://globex.com")
    assert site_of("") == ""
