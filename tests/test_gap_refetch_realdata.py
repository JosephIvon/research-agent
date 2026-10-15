"""真实数据喂入：用离线评测 case（gap_* 系列）的真实校验产物验证判定层。

`test_gap_refetch.py` 的合成用例证明判定层"逻辑正确"，但回答不了
"在真实校验产物上判得准不准"。本文件把评测夹具走一遍与工作流**完全同源**
的路径，喂给 `plan_gap_refetch`：

    llm.json（首版错版报告 + extract_sequence）
      → resolve_citations（真实证据 id 替换，与 workflow 同一函数）
      → EvidenceStore.from_extracted_data（与 workflow 同一构造）
      → verify_report（真实校验）
      → plan_gap_refetch（被测判定层）

每个 case 的形态（都是"模型记错数字"的真实场景，重生成修正）：
- gap_ungrounded   单站真缺口：59 写成 89 并引正确 id
- gap_multi_site   多站各自缺口：Acme 79→89、Beta 59→69，各引各站
- gap_mixed_unknown 幻觉 id 与真缺口混入：199 引正确 id（真缺口）+ 涨幅 10%
                   引不存在的 id（幻觉，独立成句，重生成会删）
- gap_ambiguous    一句引两家：89 同时引 Alpha 与 Beta，归属歧义
"""
import json
from pathlib import Path

from evals.fixture_source import resolve_citations
from src.agent.citations import EvidenceStore, verify_report
from src.agent.gap_refetch import (
    REASON_AMBIGUOUS_TARGET,
    REASON_ELIGIBLE,
    REASON_NO_UNGROUNDED_CLAIM,
    plan_gap_refetch,
    refetchable_gaps,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# case id -> (sites, 展示名)
_CASES = {
    "gap_ungrounded": (
        [{"key": "auditx", "url": "https://auditx.test/"}],
        ["AuditX"],
    ),
    "gap_multi_site": (
        [{"key": "acme", "url": "https://acme.test/"},
         {"key": "beta", "url": "https://beta.test/"}],
        ["Acme", "Beta"],
    ),
    "gap_mixed_unknown": (
        [{"key": "gamma", "url": "https://gamma.test/"}],
        ["Gamma"],
    ),
    "gap_ambiguous": (
        [{"key": "alpha", "url": "https://alpha.test/"},
         {"key": "beta", "url": "https://beta.test/"}],
        ["Alpha", "Beta"],
    ),
}


def _llm(cid: str):
    path = _REPO_ROOT / "evals" / "cases" / cid / "llm.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _store(llm, sites):
    """与 workflow 同源构造证据库：抽取脚本条目打上真实 source_url。"""
    items = [
        {**item, "source_url": s["url"]}
        for s in sites
        for item in llm["extract_sequence"][sites.index(s)]
    ]
    return EvidenceStore.from_extracted_data(items)


def _report(llm, sites, draft_index: int):
    return resolve_citations(llm["report"][draft_index], {"sites": sites}, llm)


def _plan(llm, sites, names, violations, max_pages=3):
    return plan_gap_refetch(
        violations, _store(llm, sites),
        target_sites=[
            {"url": s["url"], "name": n} for s, n in zip(sites, names)
        ],
        max_pages=max_pages,
    )


def _case(cid: str):
    """(llm, sites, names)——llm 从夹具加载，sites/names 来自登记表。"""
    sites, names = _CASES[cid]
    return _llm(cid), sites, names


def _verify(llm, sites, draft_index):
    return verify_report(_report(llm, sites, draft_index), _store(llm, sites))


def test_real_first_draft_ungrounded_feeds_planner_eligible():
    """单站真缺口（59→89）：真实校验判 ungrounded → 判定层判补抓。"""
    llm, sites, names = _case("gap_ungrounded")
    res = _verify(llm, sites, 0)
    assert res["passed"] is False
    ugs = [v for v in res["violations"] if v["type"] == "ungrounded_claim"]
    assert len(ugs) == 1
    assert ugs[0]["missing_numbers"] == ["89"]
    assert ugs[0]["cited_ids"], "结构化字段必须齐全"

    decision = _plan(llm, sites, names, res["violations"])

    assert decision.eligible is True
    assert decision.reason == REASON_ELIGIBLE
    assert decision.page_budget == 1
    assert [(c.competitor_url, c.missing_numbers, c.claim_count)
            for c in decision.candidates] == [("auditx.test", ["89"], 1)]


def test_real_multi_site_both_gaps_become_candidates():
    """多站各自缺口：真实校验产出两条 ungrounded → 判定层各成一候选。"""
    llm, sites, names = _case("gap_multi_site")
    res = _verify(llm, sites, 0)
    assert res["passed"] is False
    ugs = [v for v in res["violations"] if v["type"] == "ungrounded_claim"]
    assert len(ugs) == 2, f"应两站各一处缺口，实得 {len(ugs)}"
    # 每站的缺数字段都能对上（89 在 acme、69 在 beta）
    assert {"89", "69"} == {m for u in ugs for m in u["missing_numbers"]}

    decision = _plan(llm, sites, names, res["violations"])

    assert decision.eligible is True
    assert decision.page_budget == 2
    got = [(c.competitor_url, c.missing_numbers, c.claim_count, c.per_candidate_pages)
           for c in decision.candidates]
    # claim_count 相同按站点名稳定排序；每站分到 1 页
    assert got == [("acme.test", ["89"], 1, 1), ("beta.test", ["69"], 1, 1)], got


def test_real_mixed_unknown_keeps_true_gap_eligible():
    """幻觉 id 与真缺口混入：unknown 独立成句不污染真缺口的归属判定。"""
    llm, sites, names = _case("gap_mixed_unknown")
    res = _verify(llm, sites, 0)
    assert res["passed"] is False
    types = sorted({v["type"] for v in res["violations"]})
    assert types == ["ungrounded_claim", "unknown_citation"], types
    ugs = [v for v in res["violations"] if v["type"] == "ungrounded_claim"]
    assert len(ugs) == 1 and ugs[0]["missing_numbers"] == ["199"]

    decision = _plan(llm, sites, names, res["violations"])

    # unknown_citation 被看到但不参与判定；真缺口照常 eligible
    assert decision.seen_violation_types.get("unknown_citation") == 1
    assert decision.eligible is True
    assert decision.reason == REASON_ELIGIBLE
    assert [(c.competitor_url, c.missing_numbers) for c in decision.candidates] \
        == [("gamma.test", ["199"])]


def test_real_ambiguous_claim_is_skipped():
    """一句引两家：站点归属歧义 → 判定层明确不补抓（不猜）。"""
    llm, sites, names = _case("gap_ambiguous")
    res = _verify(llm, sites, 0)
    assert res["passed"] is False
    ugs = [v for v in res["violations"] if v["type"] == "ungrounded_claim"]
    assert len(ugs) == 1
    assert len(ugs[0]["cited_ids"]) == 2, "一句话引了两家证据"

    decision = _plan(llm, sites, names, res["violations"])

    assert decision.eligible is False
    assert decision.reason == REASON_AMBIGUOUS_TARGET
    assert decision.skipped.get(REASON_AMBIGUOUS_TARGET) == 1
    assert decision.candidates == []


def test_real_repaired_drafts_are_all_not_eligible():
    """四个 case 的对版（数字修正、幻觉删除、歧义拆分）：校验通过 → 都不补抓。"""
    for cid, (sites, names) in _CASES.items():
        llm = _llm(cid)
        res = _verify(llm, sites, 1)
        assert res["passed"] is True, f"{cid} 对版必须校验通过"
        decision = _plan(llm, sites, names, res["violations"])
        assert decision.eligible is False, f"{cid} 对版不应触发补抓"
        assert decision.reason == REASON_NO_UNGROUNDED_CLAIM, f"{cid}: {decision.reason}"


# --------------------------------------------------------------------------
# 「可补抓的缺口」在真实校验产物上的行为
# --------------------------------------------------------------------------

def test_real_gap_is_refetchable_only_when_the_number_lands_on_a_page():
    """真实错版（59 写成 89）的缺口 89：补抓页有 89 才算可补抓。

    反例同样重要：补抓页只有 59（站点的真值）时**不可**补抓——
    那正是实测里占 42% 的「站点上压根没有这个数」，此时再重生成只会让模型再编一个。
    """
    llm, sites, names = _case("gap_ungrounded")
    violations = _verify(llm, sites, 0)["violations"]
    ungrounded = [v for v in violations if v["type"] == "ungrounded_claim"]
    assert ungrounded[0]["missing_numbers"] == ["89"]

    assert refetchable_gaps(violations, ["专业版 89 元/席位，含审计日志。"]) == {
        violations.index(ungrounded[0]): ["89"]
    }

    assert refetchable_gaps(violations, ["专业版 59 元/席位，含审计日志。"]) == {}


def test_real_ambiguous_gap_is_refetchable_but_still_never_fetched():
    """歧义违规的**数字**仍可能可补抓——归属歧义是选页判定的事，不是这里的事。

    两道闸门各管一件事：先判该不该为这个站点抓（`plan_gap_refetch`），
    再判抓到的页面里到底有没有料（`refetchable_gaps`）。混在一起就会互相掩盖。
    """
    llm, sites, names = _case("gap_ambiguous")
    violations = _verify(llm, sites, 0)["violations"]
    ungrounded = [v for v in violations if v["type"] == "ungrounded_claim"]
    assert ungrounded[0]["missing_numbers"] == ["89"]

    assert _plan(llm, sites, names, violations).reason == REASON_AMBIGUOUS_TARGET
    assert refetchable_gaps(violations, ["Alpha 企业版 89 美元。"]) == {
        violations.index(ungrounded[0]): ["89"]
    }


def test_real_repaired_draft_has_no_refetchable_gap():
    """对版校验通过 → violations 里没有 ungrounded → 可补抓集合必为空。"""
    for cid, (sites, names) in _CASES.items():
        llm = _llm(cid)
        violations = _verify(llm, sites, 1)["violations"]
        assert refetchable_gaps(violations, ["89 59 199 全部出现在这一页。"]) == {}, cid


