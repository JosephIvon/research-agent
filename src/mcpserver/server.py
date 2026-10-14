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
from src.agent.tool_specs import build_default_tools, generate_research_report_tool
from src.agent.tools import ToolRegistry

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
    generate_research_report 的描述/schema 同样住在 src/agent/tool_specs.py，
    这里只补执行策略（handler 与墙钟相关超时）。三个工具都只读。
    """
    workspace: Dict[str, Any] = {}
    # 只为借 web_search/fetch_site 的定义；两者不依赖 LLM（extract_facts 不暴露）
    base = build_default_tools(None, workspace)
    registry = ToolRegistry()
    for name in ("web_search", "fetch_site"):
        registry.add(base.get(name))

    registry.add(generate_research_report_tool(
        _generate_research_report,
        max(300.0, AGENT_WALL_CLOCK_BUDGET * 2),
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


def mcp_sdk_available() -> bool:
    """官方 MCP SDK 是否可用。供 server.py 的就绪探针与降级路径查询。"""
    return _SDK_AVAILABLE


MCP_UNAVAILABLE_REMEDY = "uv add 'mcp>=2.3' 后重启服务"


def _unavailable_asgi_app():
    """SDK 缺失时的占位子应用：任何 HTTP 请求回 503 + 可执行的修复建议。

    **刻意不 raise**：`app.mount("/mcp", ...)` 写在 `server.py` 的模块顶层，
    一旦这里抛异常，`import server` 就失败——实测把 5 个测试文件打成 collection
    error，整个后端套件跑不起来。

    MCP 是**可选的对外能力**（外部 MCP 客户端调搜索/抓取/报告）。它缺席时主链路
    （报告生成、任务队列、引用校验）跟它毫无关系，让整个 API 为此起不来代价不对等。
    可用性没有被藏起来：`/health/ready` 的 `mcp` 段如实报告，`/mcp` 本身回 503。
    """
    body = json.dumps({
        "error": "mcp_unavailable",
        "message": "mcp SDK 未安装，MCP 接口不可用",
        "remedy": MCP_UNAVAILABLE_REMEDY,
    }, ensure_ascii=False).encode("utf-8")

    async def _app(scope, receive, send):
        if scope.get("type") != "http":
            return
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    return _app


def create_mcp_asgi_app(registry: Optional[ToolRegistry] = None):
    """可挂载的 ASGI 子应用（streamable-http，无状态）。

    挂载到 FastAPI：`app.mount("/mcp", create_mcp_asgi_app())`。
    无状态模式不要求客户端维持会话 ID，契合"搜索/抓取/报告"的
    请求-响应形态；会话/生命周期由 SDK 的 streamable_http_app 管理。

    SDK 缺失时**返回 503 占位子应用**而不是抛异常——理由见 `_unavailable_asgi_app`。
    `create_mcp_server()` / `mcp_tool_definitions()` 仍照常 raise：调用它们意味着
    代码里有个假设"SDK 一定在"，那属于编程错误，应该立刻炸。
    """
    if not _SDK_AVAILABLE:
        return _unavailable_asgi_app()

    registry = registry or build_mcp_registry()
    server = create_mcp_server(registry)
    return server.streamable_http_app(
        streamable_http_path="/",  # 经 app.mount("/mcp", ...) 挂载后收到的是剥前缀路径
        stateless_http=True,
        custom_starlette_routes=[Route("/status", _health, methods=["GET"])],
    )
