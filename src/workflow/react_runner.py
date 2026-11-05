"""ReAct Runner 装配：预算、工具注入参数、逐轮事件。

本模块不导入 ``mvp_workflow``——默认值解析仍由调用方完成（`None` 哨兵 +
运行时读模块属性），避免定义时固化引用使旧 patch 失效。
"""
from typing import Any, Dict, Optional

from src.llm.token_budget import TaskBudget


def build_budget(task_id: str, *, max_tokens: int, max_usd: float) -> TaskBudget:
    """token / USD 双口径预算（生产二者同源，注入时以用户值为准）。"""
    return TaskBudget(task_id, max_tokens=max_tokens, max_usd=max_usd)


def build_tool_kwargs(
    *,
    crawler_factory: Optional[Any] = None,
    search_factory: Optional[Any] = None,
    url_validator: Optional[Any] = None,
    url_validator_injected: bool = False,
) -> Dict[str, Any]:
    """只把**显式注入**的网络边界透给工具链。

    无条件传会把 workflow 级默认压过 tool_specs 侧的 monkeypatch——两套替换点
    本就服务不同场景：legacy 管线看 workflow，react 看 tool_specs。
    """
    kwargs: Dict[str, Any] = {}
    if crawler_factory is not None:
        kwargs["crawler_factory"] = crawler_factory
    if search_factory is not None:
        kwargs["search_factory"] = search_factory
    if url_validator_injected:
        kwargs["url_validator"] = url_validator
    return kwargs


def make_step_reporter(emit) -> Any:
    """每轮工具调用实时外发进度，SSE 订阅者据此看到模型正在做什么。"""

    def _on_step(step) -> None:
        tool_names = "、".join(tc.get("name") or "?" for tc in step.tool_calls) or "（无工具调用）"
        emit(
            "agent_step", "agent", "running",
            f"第 {step.index} 轮：{tool_names}",
            payload={
                "step": step.index,
                "thought": step.thought,
                "tool_calls": step.tool_calls,
                "stop_reason": getattr(step, "stop_reason_if_any", None),
            },
        )

    return _on_step


def assemble_runner(
    *,
    runner_factory,
    tools_builder,
    llm_client,
    workspace: Dict[str, Any],
    budget: TaskBudget,
    tool_kwargs: Dict[str, Any],
    token_budget: Optional[int],
    max_steps: Optional[int],
    wall_clock_budget: Optional[int],
    on_step,
):
    """构造 Runner；budget 同时透给工具闭包。

    extract_facts 的逐页 LLM 调用是单任务最大的 token 开销，不接进 TaskBudget
    的话 token/USD 两道守卫会按"只有 ReAct 轮"的账放行，系统性低估。
    """
    return runner_factory(
        llm_client,
        tools=tools_builder(llm_client, workspace, budget=budget, **tool_kwargs),
        workspace=workspace,
        budget=budget,
        # token_budget 与 TaskBudget 同源（M6 双口径），None 时 Runner 落 config 默认。
        token_budget=token_budget,
        max_steps=max_steps,
        wall_clock_budget=wall_clock_budget,
        on_step=on_step,
    )
