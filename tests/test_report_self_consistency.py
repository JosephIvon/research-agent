"""报告自洽性守卫：报告里出现的东西不能被报告自己否定。

回归背景：补抓（数值/维度两条路径）把新证据写进 state["extracted_data"]，
但**补抓前算出来的**校验结果与数据完整度被原样带进重生成——于是报告开头写
「没有任何一条数据包含数值，报告将无法给出带数字的可核验结论」，
正文却给出了补抓带回的价格；附录自评表还标着该维度「❌ 缺失」。
下游 judge 只能照着这份自相矛盾扣分（16/20 个 case 受影响）。
"""
import json

from evals import runner as eval_runner
from evals.fixture_source import load_manifest


def _run_gap_refetch(tmp_path):
    manifest = load_manifest(verify_digests=True)
    case = next(c for c in manifest["cases"] if c["id"] == "gap_refetch")
    llm_spec = json.loads(
        (eval_runner.CASES_DIR / case["id"] / "llm.json").read_text(encoding="utf-8"))
    out = eval_runner.run_legacy_case(case, llm_spec, tmp_path)
    return out["state"]


def test_report_does_not_deny_the_numbers_it_prints(tmp_path):
    """报告自称没有数值、正文却给了价格 → 自相矛盾，必须不存在。"""
    state = _run_gap_refetch(tmp_path)
    report = state["report_final"]
    assert "79 元/月" in report, "夹具里补抓带回的价格没进报告，自选用例失效"
    assert "没有任何一条数据包含数值" not in report
    assert "数据缺失维度：定价信息" not in report


def test_appendix_self_rating_matches_the_body(tmp_path):
    """附录自评表不能把正文已经给了内容的维度标成「❌ 缺失」。"""
    state = _run_gap_refetch(tmp_path)
    report = state["report_final"]
    pricing_rows = [l for l in report.splitlines() if l.startswith("| 定价信息")]
    assert pricing_rows, "报告里没有定价自评行"
    assert all("❌" not in l for l in pricing_rows), pricing_rows
    assert "定价信息" not in (state.get("missing_dimensions") or [])
