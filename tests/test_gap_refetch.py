"""缺口定向补抓的判定测试（`plan_gap_refetch`）。

这一层是纯函数：不联网、不调 LLM，构造违规清单和证据库就能把每条边走一遍。

**判定层刻意不做语义推断。** 它只回答「哪个竞品还有未解决的数值缺口、
按缺口密度排序、值不值得再花几页」。它**不**回答「该抓哪个页面」——
那需要业务先验，而任何这类先验在开放领域都无法穷举（见模块 docstring）。
凡是需要词表或语义才能成立的断言，都不写在这里。
"""
from src.agent.citations import EvidenceStore, verify_report
from src.agent.gap_refetch import (
    REASON_AMBIGUOUS_TARGET,
    REASON_ELIGIBLE,
    REASON_NO_UNGROUNDED_CLAIM,
    REASON_PAGE_BUDGET_ZERO,
    REASON_UNRESOLVABLE_CITATION,
    REASON_VIOLATION_NOT_STRUCTURED,
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
SITES = [{"url": ACME, "name": "Acme"}, {"url": GLOBEX, "name": "Globex"}]


# --------------------------------------------------------------------------
# 违规类型分流：第一版只处理 ungrounded_claim
# --------------------------------------------------------------------------

def test_only_ungrounded_claim_triggers_refetch():
    """另两类违规不触发补抓，理由要说清是「没有候选违规」而不是「有但被拒」。"""
    store, _ = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch([
        {"type": "unknown_citation", "detail": "引用了不存在的证据 id [e:deadbeef]"},
        {"type": "uncited_claim", "detail": "含数值的结论未标注证据", "kind": "plain"},
    ], store, SITES)

    assert d.eligible is False
    assert d.reason == REASON_NO_UNGROUNDED_CLAIM
    assert d.candidates == []
    assert d.seen_violation_types == {"unknown_citation": 1, "uncited_claim": 1}


def test_real_gap_is_eligible():
    """真缺口：目标站点上确有未解决的数值主张，应进入补抓候选。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{ACME}/home", "features", "支持 SSO 与审计日志"),
    ])

    d = plan_gap_refetch([_ungrounded([ids["pricing"]], ["99"])], store, SITES)

    assert d.eligible is True, d.reason
    assert d.reason == REASON_ELIGIBLE
    assert len(d.candidates) == 1
    c = d.candidates[0]
    assert c.competitor_url == "acme.com"
    assert c.competitor_name == "Acme"
    assert c.claim_count == 1
    # missing_numbers 只用于观测，判定过程不读它——但要如实带出供事后测量
    assert c.missing_numbers == ["99"]


# --------------------------------------------------------------------------
# 竞品归属（纯结构性：只看所引证据属于哪个站点）
# --------------------------------------------------------------------------

def test_citation_spanning_two_competitors_is_ambiguous():
    """一句同时引了 A、B 两家的证据 → 归属不清，安全跳过。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "入门版 31 元/月"),
    ])
    globex_id = _globex_id(store)

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"], globex_id], ["99"])], store, SITES)

    assert d.eligible is False
    assert d.reason == REASON_AMBIGUOUS_TARGET
    assert d.skipped.get(REASON_AMBIGUOUS_TARGET) == 1


def _globex_id(store):
    for ev in store.evidences:
        if ev.url.startswith(GLOBEX):
            return ev.evidence_id
    raise AssertionError("夹具缺 globex 证据")


def test_evidence_without_source_url_is_ambiguous():
    """证据没标来源 → 无从归属，跳过。"""
    store, ids = _store([("", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch([_ungrounded([ids["pricing"]], ["99"])], store, SITES)

    assert d.eligible is False
    assert d.reason == REASON_AMBIGUOUS_TARGET


def test_site_outside_target_scope_is_ambiguous():
    """证据来源不在本次任务的目标站点清单内 → 不擅自替它补抓。"""
    store, _ = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "入门版 31 元/月"),
    ])

    d = plan_gap_refetch(
        [_ungrounded([_globex_id(store)], ["99"])],
        store, [{"url": ACME, "name": "Acme"}],   # 只声明 Acme
    )

    assert d.eligible is False
    assert d.reason == REASON_AMBIGUOUS_TARGET


def test_unresolvable_citation_id_is_reported():
    """违规里的 id 在 store 里查不到 → 不同源，不猜。"""
    store, _ = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch([_ungrounded(["deadbeef1234"], ["99"])], store, SITES)

    assert d.eligible is False
    assert d.reason == REASON_UNRESOLVABLE_CITATION


# --------------------------------------------------------------------------
# 结构化字段缺失：必须显式失败，不能靠解析中文句子猜
# --------------------------------------------------------------------------

def test_missing_structured_fields_is_reported_not_guessed():
    """字段没了就判不可用——绝不回退去解析中文 detail 猜缺口。"""
    store, _ = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch([
        {"type": "ungrounded_claim",
         "detail": "数值 99 在所引证据原文中找不到：某产品专业版售价 99 元/月"},
    ], store, SITES)

    assert d.eligible is False
    assert d.reason == REASON_VIOLATION_NOT_STRUCTURED
    assert d.skipped.get(REASON_VIOLATION_NOT_STRUCTURED) == 1


# --------------------------------------------------------------------------
# 排序：缺口多的竞品优先（纯结构信号）
# --------------------------------------------------------------------------

def test_competitor_with_more_gaps_is_prioritised():
    """缺口条数多的竞品排在前面——按数量排，不按语义排。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "入门版 31 元/月"),
    ])
    acme_id = [ev.evidence_id for ev in store.evidences
               if ev.url.startswith(ACME)][0]
    globex_id = _globex_id(store)

    d = plan_gap_refetch(
        [
            _ungrounded([acme_id], ["99"]),
            _ungrounded([globex_id], ["88"]),
            _ungrounded([globex_id], ["77"]),
        ],
        store, SITES,
    )

    assert d.eligible is True
    assert [c.competitor_url for c in d.candidates] == ["globex.com", "acme.com"]
    assert d.candidates[0].claim_count == 2
    assert d.candidates[1].claim_count == 1


def test_tie_is_broken_deterministically():
    """缺口数相同时按站点标识排序，保证同一输入永远同一输出。"""
    store, _ = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "入门版 31 元/月"),
    ])
    acme_id = [ev.evidence_id for ev in store.evidences
               if ev.url.startswith(ACME)][0]

    d = plan_gap_refetch(
        [_ungrounded([acme_id], ["99"]),
         _ungrounded([_globex_id(store)], ["88"])],
        store, SITES,
    )

    assert [c.competitor_url for c in d.candidates] == ["acme.com", "globex.com"]
    # 再跑一次必须完全一致
    d2 = plan_gap_refetch(
        [_ungrounded([acme_id], ["99"]),
         _ungrounded([_globex_id(store)], ["88"])],
        store, SITES,
    )
    assert [c.competitor_url for c in d2.candidates] == [c.competitor_url
                                                         for c in d.candidates]


# --------------------------------------------------------------------------
# 额度
# --------------------------------------------------------------------------

def test_zero_page_budget_short_circuits():
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])
    d = plan_gap_refetch([_ungrounded([ids["pricing"]], ["99"])], store, SITES,
                         max_pages=0)
    assert d.eligible is False
    assert d.reason == REASON_PAGE_BUDGET_ZERO
    assert d.page_budget == 0


def test_budget_capped_by_candidate_count():
    """候选数少于额度时，额度收敛到候选数，不虚报。"""
    store, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{GLOBEX}/pricing", "pricing", "入门版 31 元/月"),
    ])
    acme_id = [ev.evidence_id for ev in store.evidences
               if ev.url.startswith(ACME)][0]

    d = plan_gap_refetch(
        [_ungrounded([acme_id], ["99"]), _ungrounded([_globex_id(store)], ["88"])],
        store, SITES, max_pages=10,
    )

    assert d.eligible is True
    assert d.page_budget == 2, "额度应收敛到候选数"
    assert all(c.per_candidate_pages >= 1 for c in d.candidates)


# --------------------------------------------------------------------------
# 聚合：同一站点的多个缺口共用一次抓取
# --------------------------------------------------------------------------

def test_same_site_gaps_are_merged_and_numbers_deduped():
    store, ids = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])

    d = plan_gap_refetch(
        [_ungrounded([ids["pricing"]], ["99"]),
         _ungrounded([ids["pricing"]], ["199"]),
         _ungrounded([ids["pricing"]], ["99"])],
        store, SITES,
    )

    assert d.eligible is True
    assert len(d.candidates) == 1, "同站点应聚合成一个候选"
    assert d.candidates[0].claim_count == 3
    assert d.candidates[0].missing_numbers == ["99", "199"], "重复数值应去重"


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
    assert d.candidates[0].competitor_name == "Acme"


# --------------------------------------------------------------------------
# 判定层不含语义推断：回归守卫
# --------------------------------------------------------------------------

def test_decision_does_not_depend_on_evidence_dimension_keys():
    """换掉证据的 key，判定结果必须不变。

    这是「判定层不做语义推断」的结构性守卫。早先版本按 key 归维度、
    再按维度推导补抓关键词，那张表在封闭集合上能撑住，一出这个集合就漏——
    而漏的方式是**静默失效**：认不出的 key 落进 unknown，补抓方向退化成通用词，
    外部完全看不出来。这里用「改 key 不改结论」把语义从判定层彻底摘出去。
    """
    for key in ("pricing", "pricing_tier_v2", "rate_limits", "随便什么键"):
        store, ids = _store([(f"{ACME}/x", key, "入门版 29 元/月")])
        d = plan_gap_refetch([_ungrounded([ids[key]], ["99"])], store, SITES)
        assert d.eligible is True, f"key={key} 时判定结果不应改变，实得 {d.reason}"
        assert d.candidates[0].competitor_url == "acme.com"


def test_decision_does_not_depend_on_where_the_number_appears():
    """同样的数字出现在同站任意证据里，判定结果必须不变。

    早先版本会因为「数字在别的维度里」改判 already_available。那是一次语义推断：
    由「99 在站上」推出「报告说的 99 有据」，而 99 可能是限流而不是定价。
    判定层不该做这种推断——它是事后测量（`resolved_gap_count`）的职责。
    """
    baseline, _ = _store([(f"{ACME}/pricing", "pricing", "入门版 29 元/月")])
    b = plan_gap_refetch(
        [_ungrounded([baseline.evidences[0].evidence_id], ["99"])], baseline, SITES)

    with_number, ids = _store([
        (f"{ACME}/pricing", "pricing", "入门版 29 元/月"),
        (f"{ACME}/docs", "api_flow", "限流 99 次/分钟"),
    ])
    w = plan_gap_refetch([_ungrounded([ids["pricing"]], ["99"])], with_number, SITES)

    assert b.eligible == w.eligible, (
        f"同站多出一条含 99 的证据就改变判定（{b.reason} → {w.reason}），"
        "说明判定层还在做数字语义推断"
    )
    assert b.reason == w.reason


# --------------------------------------------------------------------------
# 与 verify_report 的接线：结构化字段真的产出了
# --------------------------------------------------------------------------

def test_verify_report_emits_structured_fields_on_ungrounded_claim():
    """端到端：真实报告跑一遍校验，违规必须带机器可读字段并能直接喂进判定。"""
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

    d = plan_gap_refetch(result["violations"], store, SITES)
    assert d.eligible is True


# --------------------------------------------------------------------------
# 站点标识
# --------------------------------------------------------------------------

def test_site_of_groups_pages_of_same_competitor():
    """同一竞品的不同页面必须归到同一个站点标识，否则归属会碎掉。"""
    assert site_of("https://acme.com/pricing") == "acme.com"
    assert site_of("https://ACME.com/pricing") == "acme.com"
    assert site_of("https://acme.com/docs/api") == "acme.com"
    assert site_of("https://acme.com") != site_of("https://globex.com")
    assert site_of("") == ""
