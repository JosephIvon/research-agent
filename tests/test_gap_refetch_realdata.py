"""真实数据喂入：用离线评测 case（gap_ungrounded）的真实校验产物验证判定层。

`test_gap_refetch.py` 的合成用例证明判定层"逻辑正确"，但回答不了
"在真实校验产物上判得准不准"。本文件把 gap_ungrounded 的真实夹具走一遍
与工作流**完全同源**的路径，喂给 `plan_gap_refetch`：

    llm.json（首版错版报告 + extract_sequence）
      → resolve_citations（真实证据 id 替换，与 workflow 同一函数）
      → EvidenceStore.from_extracted_data（与 workflow 同一构造）
      → verify_report（真实校验）
      → plan_gap_refetch（被测判定层）

gap_ungrounded 的设计：首版报告把 59 写成 89 并引用正确的证据 id
（模型"记错数字"的真实形态），重生成修正。因此判定层在真实数据上
必须判出：eligible=True、候选是 auditx.test、缺的数字是 89——
且 89 在已有证据里确实不存在，是"真·证据缺口"，不是"引用错证据"。
"""
import json
from pathlib import Path

from evals.fixture_source import resolve_citations
from src.agent.citations import EvidenceStore, verify_report
from src.agent.gap_refetch import (
    REASON_ELIGIBLE,
    REASON_NO_UNGROUNDED_CLAIM,
    plan_gap_refetch,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LLM_PATH = _REPO_ROOT / "evals" / "cases" / "gap_ungrounded" / "llm.json"

_SITE = {"key": "auditx", "url": "https://auditx.test/"}
_SITE_URL = _SITE["url"]


def _load_llm():
    return json.loads(_LLM_PATH.read_text(encoding="utf-8"))


def _build_store(llm):
    """与 workflow 同源构造证据库：抽取脚本条目打上真实 source_url。"""
    items = [
        {**item, "source_url": _SITE_URL}
        for item in llm["extract_sequence"][0]
    ]
    return EvidenceStore.from_extracted_data(items)


def _plan(store, violations, max_pages=3):
    return plan_gap_refetch(
        violations, store,
        target_sites=[{"url": _SITE_URL, "name": "AuditX"}],
        max_pages=max_pages,
    )


def test_real_first_draft_ungrounded_feeds_planner_eligible():
    """首版报告（59→89 记错数字）：真实校验必须判出真缺口并建议补抓。"""
    llm = _load_llm()
    store = _build_store(llm)
    wrong_report = resolve_citations(llm["report"][0], {"sites": [_SITE]}, llm)

    result = verify_report(wrong_report, store)
    assert result["passed"] is False, "首版错版报告必须校验不过"
    ugs = [v for v in result["violations"] if v["type"] == "ungrounded_claim"]
    assert len(ugs) == 1, f"真实首版应恰好 1 处 ungrounded_claim，实得 {len(ugs)}"
    assert ugs[0]["missing_numbers"] == ["89"], "错版数字是 89"
    assert ugs[0]["sentence"] and ugs[0]["cited_ids"], "结构化字段必须齐全"

    decision = _plan(store, result["violations"])

    assert decision.eligible is True
    assert decision.reason == REASON_ELIGIBLE
    assert decision.page_budget == 1
    assert len(decision.candidates) == 1
    cand = decision.candidates[0]
    assert cand.competitor_url == "auditx.test", "归属按站点 netloc"
    assert cand.competitor_name == "AuditX"
    assert cand.missing_numbers == ["89"]
    assert cand.claim_count == 1
    assert cand.per_candidate_pages == 1


def test_real_repaired_draft_is_not_eligible():
    """重生成后的对版报告（59 修正）：校验通过 → 判定层必须判不补抓。"""
    llm = _load_llm()
    store = _build_store(llm)
    good_report = resolve_citations(llm["report"][1], {"sites": [_SITE]}, llm)

    result = verify_report(good_report, store)
    assert result["passed"] is True, "对版报告必须校验通过"
    assert result["violations"] == [], "通过即无违规"

    decision = _plan(store, result["violations"])

    assert decision.eligible is False
    assert decision.reason == REASON_NO_UNGROUNDED_CLAIM
    assert decision.candidates == []
