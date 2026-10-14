"""MCP 协议实现（官方 SDK 2.x）：低层 Server + streamable-http（无状态）。

挂载方式（接线批次写入 server.py，此文件不 import server，避免环）：

    from src.mcpserver import create_mcp_asgi_app
    app.mount("/mcp", create_mcp_asgi_app())

约束：`/mcp` 不得加入 server.PUBLIC_PATHS（未认证请求必须被外层拦下）。

SDK 版本：按 2.3.0 的 API 写（构造器回调 on_list_tools/on_call_tool、
streamable_http_app），pyproject 下限相应锁 >=2.3。
"""
import json
from typing import Any, Dict, List, Optional

from starlette.routing import Route

from src.agent.config import AGENT_WALL_CLOCK_BUDGET
from src.agent.runner import ReActRunner
from src.agent.tool_specs import build_default_tools
from src.agent.tools import Tool, ToolRegistry

try:  # 官方 MCP Python SDK（uv add "mcp>=2.3" 引入）
    from mcp import types as mcp_types
    from mcp.server import Server
    _SDK_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖缺失时保留可导入性（测试会显式跳过）
    _SDK_AVAILABLE = False


async def _generate_research_report(query: str, target_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    """跑一次有界 ReAct 调研并返回报告。

    内部 Runner 持有**独立**的工具注册表（不含本工具自身），防止模型递归
    调用报告生成。
    """
    from src.llm import get_llm_client

    llm = get_llm_client()
    workspace: Dict[str, Any] = {}
    runner_registry = build_default_tools(llm, workspace)
    result = await ReActRunner(llm, tools=runner_registry, workspace=workspace).run(
        query, target_urls=target_urls
    )
    return {
        "report": result.answer,
        "stop_reason": result.stop_reason,
        "steps": len(result.steps),
        "usage": result.usage,
        "cost_report": result.cost_report,
        "elapsed_s": result.elapsed_s,
    }


def build_mcp_registry() -> ToolRegistry:
    """MCP 暴露的工具集：web_search / fetch_site / generate_research_report。

    web_search 与 fetch_site 复用 M3 注册表定义（schema 与实现同源）；
    generate_research_report 是新增的编排工具，同样只读。
    """
    workspace: Dict[str, Any] = {}
    # 只为借 web_search/fetch_site 的定义；两者不依赖 LLM（extract_facts 不暴露）
    base = build_default_tools(None, workspace)
    registry = ToolRegistry()
    for name in ("web_search", "fetch_site"):
        registry.add(base.get(name))

    registry.add(Tool(
        name="generate_research_report",
        description=(
            "执行一次完整的竞品调研并返回结构化报告（有界 ReAct：自主搜索/抓取/"
            "抽取后综合成文，含引用与成本报告）。适合需要多步信息收集的问题；"
            "单点事实查询请优先用 web_search / fetch_site。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "调研问题，尽量具体"},
                "target_urls": {
                    "type": "array", "items": {"type": "string"},
                    "description": "可选：优先调研的站点 URL 列表",
                },
            },
            "required": ["query"],
        },
        handler=_generate_research_report,
        timeout=max(300.0, AGENT_WALL_CLOCK_BUDGET * 2),
        url_params=(),
    ))
    return registry


def mcp_tool_definitions(registry: ToolRegistry) -> List["mcp_types.Tool"]:
    """由注册表生成 MCP Tool 定义——schema 单一来源的直接体现。"""
    if not _SDK_AVAILABLE:
        raise RuntimeError("mcp SDK 未安装：uv add 'mcp>=2.3'")
    return [
        mcp_types.Tool(
            name=tool.name,
            description=tool.description,
            input_schema=tool.parameters,
        )
        for tool in (registry.get(name) for name in registry.names())
    ]


def create_mcp_server(registry: ToolRegistry) -> "Server":
    """低层 MCP Server：list_tools/call_tool 回调直接绑定注册表。"""
    if not _SDK_AVAILABLE:
        raise RuntimeError("mcp SDK 未安装：uv add 'mcp>=2.3'")

    async def _on_list_tools(context, params) -> mcp_types.ListToolsResult:
        return mcp_types.ListToolsResult(tools=mcp_tool_definitions(registry))

    async def _on_call_tool(context, params) -> mcp_types.CallToolResult:
        # 一律经注册表执行：白名单/校验/超时/统一返回结构零旁路
        result = await registry.execute(params.name, dict(params.arguments or {}))
        text = json.dumps(result, ensure_ascii=False, default=str)
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=text)]
        )

    return Server(
        "research-agent",
        version="1.0.0",
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


async def _health(request):  # 挂载路径探测用，不暴露任何状态
    from starlette.responses import JSONResponse
    return JSONResponse({"service": "research-agent-mcp", "stateless": True})


def create_mcp_asgi_app(registry: Optional[ToolRegistry] = None):
    """可挂载的 ASGI 子应用（streamable-http，无状态）。

    挂载到 FastAPI：`app.mount("/mcp", create_mcp_asgi_app())`。
    无状态模式不要求客户端维持会话 ID，契合"搜索/抓取/报告"的
    请求-响应形态；会话/生命周期由 SDK 的 streamable_http_app 管理。
    """
    if not _SDK_AVAILABLE:
        raise RuntimeError("mcp SDK 未安装：uv add 'mcp>=2.3'")

    registry = registry or build_mcp_registry()
    server = create_mcp_server(registry)
    return server.streamable_http_app(
        streamable_http_path="/",  # 经 app.mount("/mcp", ...) 挂载后收到的是剥前缀路径
        stateless_http=True,
        custom_starlette_routes=[Route("/status", _health, methods=["GET"])],
    )
