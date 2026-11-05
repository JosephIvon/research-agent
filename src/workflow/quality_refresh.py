"""质量与可追溯性重算：补抓后刷新自评表与校验结果。

从 mvp_workflow 抽出的纯叶子职责：只对传入的 state 与 coordinator 做计算，
不依赖工作流实例，也不反向导入 mvp_workflow。
"""
from typing import Any, Dict


def refresh_quality_report(state: Dict[str, Any]):
    """按当前竞品数据重算数据完整度（补抓后必须重算）。

    附录自评表必须与报告正文看到的数据一致，否则报告会出现
    「正文有定价、附录说定价缺失」的自相矛盾——下游 judge 只能照着错账扣分。
    """
    from src.tools.quality_scorer import QualityScorer

    quality_report = QualityScorer(state.get("competitors", [])).evaluate()
    state["quality_score"] = quality_report.overall_score
    state["quality_grade"] = quality_report.grade
    state["missing_dimensions"] = quality_report.missing_dimensions
    return quality_report


async def refresh_verification(coordinator, state: Dict[str, Any]):
    """补抓后重算可追溯性校验（补抓前算的结果已经过期）。

    报告开头的「数据质量提示」用的就是它：补抓带回的数字不进旧结果，
    报告就会自称「没有任何一条数据包含数值」却给出了价格——同样是自相矛盾。
    """
    verification_result = await coordinator.assess_extracted_data(state["extracted_data"])
    state["verification_score"] = verification_result["score"]
    state["verification_passed"] = verification_result["passed"]
    state["verification_warnings"] = verification_result["warnings"]
    state["source_coverage"] = verification_result["source_coverage"]
    state["evidence_count"] = verification_result["evidence_count"]
    state["numeric_evidence_count"] = verification_result["numeric_evidence_count"]
    return verification_result
