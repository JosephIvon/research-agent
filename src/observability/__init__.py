"""可观测层：trace 关联、span 生命周期。

设计红线（实施草案 §6-1）：**task_id 关联一律走 contextvars，禁止实例属性**。
LLM 调用已经进线程池（asyncio.to_thread），实例属性在多任务并发下必然串号；
contextvars 在 to_thread 提交时被快照复制，天然按任务隔离。
"""
from src.observability.langfuse_tracer import (
    clear_task_context,
    current_task_id,
    current_trace,
    is_enabled,
    llm_span,
    set_task_context,
    trace_url,
)

__all__ = [
    "is_enabled",
    "set_task_context",
    "clear_task_context",
    "current_task_id",
    "current_trace",
    "llm_span",
    "trace_url",
]
