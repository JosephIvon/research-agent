"""Langfuse 追踪封装：LANGFUSE_ENABLED 未开启时零开销 no-op。

并发正确性：trace/上下文全部走 contextvars。
【模块】草案用 `self._current_task_id` 实例属性传 task_id——LLM 调用搬进
线程池后，多任务并发会在同一 client 实例上互相覆盖，trace 张冠李戴。
本实现从根上避开：set_task_context() 设置 ContextVar，to_thread 的执行体
自动携带提交时刻的快照。
"""
import contextvars
import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_task_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("ra_task_id", default=None)
_user_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("ra_user_id", default=None)
_trace_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("ra_trace", default=None)

_langfuse = None
_ENABLED = False


def is_enabled() -> bool:
    return _ENABLED and _langfuse is not None


def _init() -> None:
    """惰性初始化：密钥缺失/导入失败自动降级为 no-op，绝不让观测拖垮主流程。"""
    global _langfuse, _ENABLED
    if os.getenv("LANGFUSE_ENABLED", "false").lower() != "true":
        return
    try:
        from langfuse import Langfuse
        _langfuse = Langfuse(
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.getenv("LANGFUSE_SECRET_KEY"),
            host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        _ENABLED = True
        logger.info("[observability] Langfuse enabled")
    except Exception as exc:
        logger.warning("[observability] Langfuse 初始化失败，已降级为 no-op: %s", exc)
        _ENABLED = False


_init()


# --------------------------------------------------------------------------
# 任务上下文（contextvars —— 线程池安全的关联方式）
# --------------------------------------------------------------------------

def set_task_context(task_id: str, user_id: Optional[str] = None) -> None:
    _task_id_var.set(task_id)
    _user_id_var.set(user_id)


def clear_task_context() -> None:
    _task_id_var.set(None)
    _user_id_var.set(None)
    _trace_var.set(None)


def current_task_id() -> Optional[str]:
    return _task_id_var.get()


def current_trace() -> Optional[Any]:
    return _trace_var.get()


# --------------------------------------------------------------------------
# trace / span
# --------------------------------------------------------------------------

def start_trace(name: str, input: Optional[Dict[str, Any]] = None,
                task_id: Optional[str] = None, user_id: Optional[str] = None) -> Optional[Any]:
    """任务级 trace 起点。task_id 未显式给时取当前上下文（contextvars）。"""
    if not is_enabled():
        return None
    tid = task_id or current_task_id()
    if not tid:
        return None
    # 观测绝不能拖垮主流程（模块 docstring 的红线），启用态尤其要守：
    # 这段已经被接进 FastAPI 任务入口，SDK 一旦故障就会把整个调研任务打死。
    try:
        trace = _langfuse.trace(
            id=tid,
            name=name,
            user_id=user_id or _user_id_var.get(),
            input=input,
        )
    except Exception:
        logger.warning("[observability] start_trace 失败，本次运行不产生 trace", exc_info=True)
        _trace_var.set(None)
        return None
    _trace_var.set(trace)
    return trace


def end_trace(output: Optional[Dict[str, Any]] = None) -> None:
    trace = _trace_var.get()
    if trace is not None and is_enabled():
        try:
            trace.update(output=output)
        except Exception:
            logger.debug("[observability] end_trace 失败（不影响主流程）", exc_info=True)
    _trace_var.set(None)


@contextmanager
def llm_span(name: str, model: Optional[str] = None,
             model_parameters: Optional[Dict[str, Any]] = None):
    """LLM 调用 span。挂在当前上下文的 trace 下；无 trace/未启用时为空操作。

    线程池说明：调用方若在 to_thread 里执行 LLM 请求，span 的开启发生在
    提交前的上下文快照中——本上下文管理器应在 to_thread 外层包裹。
    """
    trace = _trace_var.get()
    if not is_enabled() or trace is None:
        yield None
        return
    # 同 start_trace：开启 span 失败只能放弃这次 span，不能把调用方的 LLM 请求带崩。
    try:
        span = trace.generation(
            name=name,
            model=model,
            model_parameters=model_parameters or {},
        )
    except Exception:
        logger.warning("[observability] llm_span 开启失败，跳过该 span", exc_info=True)
        span = None
    try:
        yield span
        if span is not None:
            span.end()
    except Exception as exc:
        if span is not None:
            try:
                span.end(level="ERROR", status_message=str(exc))
            except Exception:
                pass
        raise


def trace_url(task_id: Optional[str] = None) -> Optional[str]:
    """任务详情接口用：拼接 Langfuse 控制台跳转链接（未启用返回 None）。"""
    if not is_enabled():
        return None
    host = (os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")).rstrip("/")
    return f"{host}/trace/{task_id or current_task_id()}"
