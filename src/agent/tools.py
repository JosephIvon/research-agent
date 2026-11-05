"""工具注册表：ReAct Runner / MCP Server / 评测脚本三处共用的单一来源。

硬约束（实施草案 M3，2026-11-04 扩为契约 v2）：
- **只读白名单**：注册的每个工具不得有外部副作用（不写库、不发消息、不建文档）
- **副作用声明**：`side_effects` 必须是 `read`/`write`；声明 `read_only=True` 却标
  `side_effects="write"` 的工具会在注册时被拒绝——这是契约校验器的牙齿
- **URL 参数二次校验**：凡入参含 URL 的工具，执行前再过一遍 `validate_fetch_url`——
  入口校验不能信任 LLM 生成的参数，SSRF 边界必须在执行点再拦一次
- **统一返回结构** `{ok, data, error, elapsed, error_code, retryable}`，
  禁止各工具自定义返回形状——Runner 的收束逻辑（空结果/重复/超时判定）依赖
  统一结构才可靠。`error_code`/`retryable` 是加法字段，旧键语义不变
- **输出游标**：声明 `paginated=True` 的工具自动获得 `offset`/`limit` 参数，
  返回顶层 `nextOffset`；不传分页参数时行为与改造前完全一致
"""
import asyncio
import copy
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.agent.error_codes import (
    RETRYABLE_TOOL_ERROR_CODES,
    STANDARD_TOOL_ERROR_CODES,
    ToolContractError,
    ToolErrorCode,
)
from src.security.url_validation import UnsafeURL, validate_fetch_url

logger = logging.getLogger(__name__)

_PAGINATION_KEYS = ("offset", "limit")


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]          # JSON Schema（OpenAI function 格式的 parameters）
    handler: Callable[..., Any]          # async；入参即 schema 声明的字段
    timeout: float = 30.0
    read_only: bool = True
    url_params: Tuple[str, ...] = ()    # 需要二次 SSRF 校验的参数名
    # 契约 v2（2026-11-04）：副作用 / 幂等 / 错误码集合 / 分页
    side_effects: str = "read"          # "read" | "write"
    idempotent: bool = True
    error_codes: Tuple[str, ...] = STANDARD_TOOL_ERROR_CODES
    paginated: bool = False
    page_field: Optional[str] = None    # 分页切片所在字段；None 表示 data 本身是列表

    def to_schema(self) -> Dict[str, Any]:
        """OpenAI function calling 格式的工具定义。"""
        parameters = copy.deepcopy(self.parameters)
        if self.paginated:
            properties = parameters.setdefault("properties", {})
            properties.setdefault(
                "offset", {"type": "integer", "description": "已消费条数，默认 0"}
            )
            properties.setdefault(
                "limit", {"type": "integer", "description": "本页最多返回条数，默认不截断"}
            )
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


class ToolRegistry:
    """按名称索引的工具集合；每个 Runner 实例持有一份（handler 可带 workspace 闭包）。"""

    def __init__(self, url_validator: Optional[Callable[[str], str]] = None):
        self._tools: Dict[str, Tool] = {}
        # URL 参数二次 SSRF 校验的注入口：生产走 validate_fetch_url，评测注入
        # passthrough 让夹具 .test 域名过闸（替代 monkeypatch 模块属性）。
        self._url_validator = url_validator if url_validator is not None else validate_fetch_url

    def add(self, tool: Tool) -> None:
        if not tool.read_only:
            raise ValueError(f"工具 {tool.name!r} 不是只读工具，拒绝注册——白名单只收只读")
        if tool.side_effects not in ("read", "write"):
            raise ValueError(
                f"工具 {tool.name!r} 的 side_effects={tool.side_effects!r} 非法，只允许 read/write"
            )
        if tool.side_effects == "write":
            # 契约校验器的牙齿：声明只读却实际写，必须在注册时被拦下。
            raise ValueError(
                f"工具 {tool.name!r} 声明 read_only=True 但 side_effects='write'——"
                "只读白名单不允许写副作用"
            )
        unknown_codes = set(tool.error_codes) - set(STANDARD_TOOL_ERROR_CODES)
        if unknown_codes:
            raise ValueError(
                f"工具 {tool.name!r} 声明了未登记的错误码: {sorted(unknown_codes)}"
            )
        if tool.name in self._tools:
            raise ValueError(f"工具重名: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools)

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.to_schema() for t in self._tools.values()]

    async def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具并归一化结果。任何失败都以 ok=False 返回，不抛异常——
        工具失败是模型可以观察到并自我纠正的信息，不是 Runner 的终止条件。
        """
        started = time.monotonic()
        tool = self.get(name)
        if tool is None:
            return self._failure(
                ToolErrorCode.UNKNOWN_TOOL,
                f"unknown tool: {name}（可用: {', '.join(self.names())}）",
                0.0,
            )

        page: Optional[Tuple[int, Optional[int]]] = None
        handler_arguments = arguments
        if isinstance(arguments, dict) and tool.paginated:
            page, handler_arguments, page_error = self._extract_pagination(arguments)
            if page_error:
                return self._failure(ToolErrorCode.INVALID_ARGUMENTS, page_error, 0.0)

        error = self._validate_arguments(tool, handler_arguments)
        if error:
            code, message = error
            return self._failure(code, message, 0.0)

        try:
            result = await asyncio.wait_for(
                _call_handler(tool.handler, handler_arguments), timeout=tool.timeout
            )
        except asyncio.TimeoutError:
            return self._failure(
                ToolErrorCode.TIMEOUT,
                f"工具 {name} 超时（>{tool.timeout}s）",
                round(time.monotonic() - started, 4),
            )
        except ToolContractError as exc:
            # handler 声明的失败：错误码与消息由业务代码给定，非兜底
            logger.info("工具 %s 契约失败: %s", name, exc.message)
            return self._failure(
                exc.code,
                exc.message,
                round(time.monotonic() - started, 4),
            )
        except Exception as exc:  # 工具内部异常一律转为可观察的失败结果
            logger.warning("工具 %s 执行失败: %s", name, exc)
            return self._failure(
                ToolErrorCode.EXECUTION_ERROR,
                f"{type(exc).__name__}: {exc}",
                round(time.monotonic() - started, 4),
            )

        response: Dict[str, Any] = {
            "ok": True,
            "data": result,
            "error": None,
            "elapsed": round(time.monotonic() - started, 4),
            "error_code": None,
            "retryable": False,
        }
        if tool.paginated:
            sliced, next_offset = self._slice_result(tool, result, page)
            response["data"] = sliced
            response["nextOffset"] = next_offset
        return response

    def _validate_arguments(
        self, tool: Tool, arguments: Any
    ) -> Optional[Tuple[ToolErrorCode, str]]:
        if not isinstance(arguments, dict):
            return (
                ToolErrorCode.INVALID_ARGUMENTS,
                f"参数必须是 JSON 对象，实际: {type(arguments).__name__}",
            )
        for required in tool.parameters.get("required", []):
            if required not in arguments or arguments[required] in (None, ""):
                return (ToolErrorCode.INVALID_ARGUMENTS, f"缺少必填参数: {required}")
        # URL 参数二次过 SSRF 校验——LLM 生成的 URL 不受任何信任
        for param in tool.url_params:
            value = arguments.get(param)
            if not value:
                continue
            try:
                self._url_validator(value)
            except UnsafeURL as exc:
                return (
                    ToolErrorCode.UNSAFE_URL,
                    f"参数 {param} 的 URL 被安全策略拒绝: {exc}",
                )
            except ValueError as exc:
                return (ToolErrorCode.UNSAFE_URL, f"参数 {param} 的 URL 非法: {exc}")
        return None

    @staticmethod
    def _extract_pagination(
        arguments: Dict[str, Any],
    ) -> Tuple[Optional[Tuple[int, Optional[int]]], Dict[str, Any], Optional[str]]:
        """把 offset/limit 从入参里摘出来：handler 永远拿不到分页参数。"""
        offset = arguments.get("offset")
        limit = arguments.get("limit")
        if offset is None and limit is None:
            return None, arguments, None
        try:
            offset = 0 if offset is None else int(offset)
            limit = None if limit is None else int(limit)
        except (TypeError, ValueError):
            return None, arguments, "offset/limit 必须是整数"
        if offset < 0:
            return None, arguments, "offset 必须 >= 0"
        if limit is not None and limit <= 0:
            return None, arguments, "limit 必须 > 0"
        handler_arguments = {k: v for k, v in arguments.items() if k not in _PAGINATION_KEYS}
        return (offset, limit), handler_arguments, None

    @staticmethod
    def _slice_result(
        tool: Tool, result: Any, page: Optional[Tuple[int, Optional[int]]]
    ) -> Tuple[Any, Optional[int]]:
        """按 offset/limit 切片；返回 (本页数据, nextOffset)。

        未传分页参数时原样返回（nextOffset=None），保证改造前后同一调用同结果。
        传了才切：列表工具直接切 data；字典工具切 `page_field`，并返回浅拷贝，
        避免把游标写进 handler 的缓存对象（discover_competitors 会复用 workspace）。
        """
        if page is None:
            return result, None
        offset, limit = page
        if tool.page_field is None:
            items = result
            wrapped = None
        elif isinstance(result, dict):
            items = result.get(tool.page_field) or []
            wrapped = result
        else:
            return result, None
        if not isinstance(items, list):
            return result, None
        end = len(items) if limit is None else offset + limit
        page_items = items[offset:end]
        next_offset = end if end < len(items) else None
        if wrapped is None:
            return page_items, next_offset
        payload = dict(wrapped)
        payload[tool.page_field] = page_items
        return payload, next_offset

    @staticmethod
    def _failure(code: ToolErrorCode, message: str, elapsed: float) -> Dict[str, Any]:
        return {
            "ok": False,
            "data": None,
            "error": message,
            "elapsed": elapsed,
            "error_code": code.value,
            "retryable": code.value in RETRYABLE_TOOL_ERROR_CODES,
        }


async def _call_handler(handler: Callable, arguments: Dict[str, Any]) -> Any:
    """兼容 sync/async handler；多余字段不透传（schema 之外的键直接丢弃）。"""
    if inspect.iscoroutinefunction(handler):
        return await handler(**arguments)
    return await asyncio.to_thread(handler, **arguments)


def result_is_empty(result: Dict[str, Any]) -> bool:
    """统一结构下的"空观察"判定：成功但没拿到任何数据。"""
    if not result.get("ok"):
        return False  # 失败是信息，不是空
    data = result.get("data")
    if data is None:
        return True
    if isinstance(data, (list, dict, str)) and len(data) == 0:
        return True
    return False


def dedup_key(name: str, arguments: Dict[str, Any]) -> str:
    """同工具同参数的稳定指纹（参数键序无关）。"""
    return name + "|" + json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
