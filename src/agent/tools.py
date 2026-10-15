"""工具注册表：ReAct Runner / MCP Server / 评测脚本三处共用的单一来源。

硬约束（实施草案 M3）：
- **只读白名单**：注册的每个工具不得有外部副作用（不写库、不发消息、不建文档）
- **URL 参数二次校验**：凡入参含 URL 的工具，执行前再过一遍 `validate_fetch_url`——
  入口校验不能信任 LLM 生成的参数，SSRF 边界必须在执行点再拦一次
- **统一返回结构** `{ok, data, error, elapsed}`，禁止各工具自定义返回形状——
  Runner 的收束逻辑（空结果/重复/超时判定）依赖统一结构才可靠
"""
import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.security.url_validation import UnsafeURL, validate_fetch_url

logger = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]          # JSON Schema（OpenAI function 格式的 parameters）
    handler: Callable[..., Any]          # async；入参即 schema 声明的字段
    timeout: float = 30.0
    read_only: bool = True
    url_params: Tuple[str, ...] = ()    # 需要二次 SSRF 校验的参数名

    def to_schema(self) -> Dict[str, Any]:
        """OpenAI function calling 格式的工具定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
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
            return {
                "ok": False, "data": None, "elapsed": 0.0,
                "error": f"unknown tool: {name}（可用: {', '.join(self.names())}）",
            }

        error = self._validate_arguments(tool, arguments)
        if error:
            return {"ok": False, "data": None, "elapsed": 0.0, "error": error}

        try:
            result = await asyncio.wait_for(
                _call_handler(tool.handler, arguments), timeout=tool.timeout
            )
            return {"ok": True, "data": result, "error": None,
                    "elapsed": round(time.monotonic() - started, 4)}
        except asyncio.TimeoutError:
            return {"ok": False, "data": None, "error": f"工具 {name} 超时（>{tool.timeout}s）",
                    "elapsed": round(time.monotonic() - started, 4)}
        except Exception as exc:  # 工具内部异常一律转为可观察的失败结果
            logger.warning("工具 %s 执行失败: %s", name, exc)
            return {"ok": False, "data": None, "error": f"{type(exc).__name__}: {exc}",
                    "elapsed": round(time.monotonic() - started, 4)}

    def _validate_arguments(self, tool: Tool, arguments: Dict[str, Any]) -> Optional[str]:
        if not isinstance(arguments, dict):
            return f"参数必须是 JSON 对象，实际: {type(arguments).__name__}"
        for required in tool.parameters.get("required", []):
            if required not in arguments or arguments[required] in (None, ""):
                return f"缺少必填参数: {required}"
        # URL 参数二次过 SSRF 校验——LLM 生成的 URL 不受任何信任
        for param in tool.url_params:
            value = arguments.get(param)
            if not value:
                continue
            try:
                self._url_validator(value)
            except UnsafeURL as exc:
                return f"参数 {param} 的 URL 被安全策略拒绝: {exc}"
            except ValueError as exc:
                return f"参数 {param} 的 URL 非法: {exc}"
        return None


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
