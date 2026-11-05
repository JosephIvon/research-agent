"""ReAct 终态交付：证据/引用校验装配、落盘、终态事件。

父级行为（emit / 落盘 / 校验 / 状态回写）全部由参数注入；本模块不导入
``mvp_workflow``，避免反向依赖。所有函数保持原调用顺序与事件顺序。
"""
import asyncio
from pathlib import Path
from typing import Any, Dict, List

from src.agent.citations import EvidenceStore
from src.llm import LLMError
from src.workflow.competitor_matrix import inject_section
from src.workflow.task_store import TaskEventType


async def attach_evidence_and_citation(
    state: Dict[str, Any],
    facts: List[Dict[str, Any]],
    user_query: str,
    *,
    verify_report,
    competitors_from_facts,
    derive_matrix,
    mark_low_trust,
) -> None:
    """把 workspace 事实接入同一条交付链：矩阵 → 引用校验 → 机器附录。"""
    store = EvidenceStore.from_extracted_data(facts)
    # ReAct 也走同一条交付链：矩阵 + 锚定借鉴点 + 固定速览章节。
    # 此前 react 模式只交模型终答，竞品矩阵与可借鉴点这两项结构化能力在 react
    # 下完全缺席——同一条产品能力两种模式两种结果，是最难解释的一种不一致。
    state["competitors"] = competitors_from_facts(facts)
    matrix_obj = await derive_matrix(state, user_query)
    if matrix_obj is not None and matrix_obj.rows:
        # 位置放"后"：ReAct 报告正文是模型终答，合同是报告以模型交付物开头
        # （连低可信前缀都不能加在它前面）；机器生成的速览表跟在后面。
        state["report_final"] = inject_section(
            state["report_final"], matrix_obj, position="end",
        )
    citation_result = await asyncio.to_thread(verify_report, state["report_final"], store)
    state["citation_report"] = {
        k: v for k, v in citation_result.items() if k != "violations"
    }
    state["citation_violations"] = citation_result["violations"]
    state["citation_trusted"] = citation_result["passed"]
    if not citation_result["passed"]:
        # 与 legacy 同一原则：未过校验的报告不静默交付，顶部挂低可信标记
        state["report_final"] = mark_low_trust(state["report_final"], citation_result)
    # 证据对照表进附录：校验已做完才拼，本节是确定性代码产出，挂机器附录标记
    # 免重复计数（legacy 在报告装配时拼）。
    state["report_final"] += (
        "\n\n<!--machine-appendix-->\n"
        + store.appendix_table()
        + "\n<!--/machine-appendix-->"
    )


def finish_success(
    state: Dict[str, Any],
    result,
    *,
    enforce_output,
    save_report,
    persist,
    update_status,
    emit,
) -> None:
    """出口清洗 → 落盘 → 持久化 → completed/partial 终态。"""
    enforce_output(state)
    if not str(state.get("report_final") or "").strip():
        raise LLMError("ReAct 报告在出口清洗后为空，已中止交付")
    state["sync_results"]["local_file"] = save_report(
        state["report_final"], state.get("task_id")
    )
    state["report_id"] = Path(state["sync_results"]["local_file"]).stem
    persist(state)
    state["termination_reason"] = f"react:{result.stop_reason}"
    # M6：预算耗尽但终答已产出——结论照常交付，终态标 partial 而非 completed，
    # 让前端/落库能区分"跑完"与"被预算截断"。
    if result.stop_reason == "budget_exceeded":
        state["status"] = "partial"
        emit(
            TaskEventType.PARTIAL.value, "completed", "partial",
            f"预算耗尽，按已获得的证据收敛交付（stop_reason={result.stop_reason}）",
            payload={
                "status": state["status"],
                "termination_reason": state["termination_reason"],
                "stop_reason": result.stop_reason,
                "cost_report": state["cost_report"],
                "unread_folded_evidence": state["unread_folded_evidence"],
                "unread_cited_evidence": state["unread_cited_evidence"],
            },
        )
    else:
        state["status"] = "completed"
        emit(
            "completed", "completed", "completed", "全部完成",
            payload={
                "unread_folded_evidence": state["unread_folded_evidence"],
                "unread_cited_evidence": state["unread_cited_evidence"],
            },
        )
    update_status(state)


def finish_failure(state: Dict[str, Any], result, *, update_status, emit) -> None:
    """无终答：failed 终态 + 可读的失败说明。"""
    state["status"] = "failed"
    state["termination_reason"] = f"react:{result.stop_reason}"
    update_status(state)
    state["report_final"] = (
        f"❌ ReAct 调研未产出终答（stop_reason={result.stop_reason}，"
        f"共执行 {len(result.steps)} 轮），可查看 agent_step 事件了解已完成的工具调用。"
    )
    emit(
        "error", "agent", "failed",
        f"ReAct 未产出终答：{result.stop_reason}",
        payload={
            "stop_reason": result.stop_reason,
            "steps": len(result.steps),
            "unread_folded_evidence": state.get("unread_folded_evidence", 0),
            "unread_cited_evidence": state.get("unread_cited_evidence", 0),
        },
    )


def handle_exception(state: Dict[str, Any], exc: Exception, *, emit, update_status) -> None:
    """异常兜底：failed 终态、错误入账、事件外发。"""
    from datetime import datetime

    state["status"] = "failed"
    state["termination_reason"] = f"执行失败: {str(exc)}"
    state["quality_score"] = None
    state["quality_grade"] = None
    state["errors"].append({
        "timestamp": datetime.now(),
        "stage": "workflow",
        "error_type": type(exc).__name__,
        "message": str(exc),
        "retry_attempts": 0,
    })
    emit(
        "error", "workflow", "failed",
        f"执行失败: {str(exc)}",
        payload={"stage": "workflow", "error_type": type(exc).__name__},
    )
    update_status(state)
