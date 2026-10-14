"""MCP Server（M5）测试：schema 单一来源 + 注册表零旁路 + 真客户端端到端。

验收拆分（实施草案 M5）：
- 协议可用：用官方 SDK 的**真实 MCP 客户端**打满 initialize / tools/list /
  tools/call 一个来回——不是自测自的桩。
- 安全继承：URL 工具经 MCP 调用时，注册表的 SSRF 二次校验必须仍然生效。
- schema 一致性（复核 §3.4）：MCP 对外暴露的 schema 与 ToolRegistry 的
  定义逐字段一致，漂移即红。
"""
import asyncio
import json
import threading

import pytest

from src.agent.tools import Tool, ToolRegistry
from src.mcpserver.server import (
    create_mcp_asgi_app,
    mcp_sdk_available,
    mcp_tool_definitions,
)

# 刻意**不在模块级 importorskip("mcp")**：那会让整个文件在没装 SDK 的机器上被跳过，
# 连 /mcp 白名单这类与协议无关的用例一起失去保护。改为只给真正走协议的用例挂 skip，
# 判定复用生产代码里的同一个标志（src/mcpserver/server.py 的 _SDK_AVAILABLE）。
requires_mcp_sdk = pytest.mark.skipif(
    not mcp_sdk_available(), reason="需要官方 mcp SDK（uv add mcp）"
)


def _test_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def _echo(text: str):
        return {"echo": text}

    async def _unsafe_fetch(url: str):
        return {"never": "reached"}

    registry.add(Tool(
        name="echo",
        description="回声测试工具",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        handler=_echo,
        timeout=5.0,
    ))
    registry.add(Tool(
        name="unsafe_fetch",
        description="带 URL 参数的工具，用于验证 SSRF 校验继承",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        handler=_unsafe_fetch,
        timeout=5.0,
        url_params=("url",),
    ))
    return registry


# --------------------------------------------------------------------------
# schema 单一来源（复核 §3.4：防漂移的约束级测试）
# --------------------------------------------------------------------------

@requires_mcp_sdk
def test_mcp_tool_definitions_match_registry_exactly():
    registry = _test_registry()
    definitions = mcp_tool_definitions(registry)
    assert [d.name for d in definitions] == registry.names()

    for definition in definitions:
        tool = registry.get(definition.name)
        assert definition.description == tool.description
        assert definition.input_schema == tool.parameters, \
            f"{tool.name} 的 schema 在 MCP 层出现第二份且已漂移"


def test_mcp_registry_contains_expected_services():
    """正式注册表的三项服务与草案 M5 一致，且全部只读。"""
    from src.mcpserver.server import build_mcp_registry

    registry = build_mcp_registry()
    assert set(registry.names()) == {"web_search", "fetch_site", "generate_research_report"}
    for name in registry.names():
        assert registry.get(name).read_only is True


def test_mcp_path_is_not_in_public_paths():
    """`/mcp` 不得进入免鉴权白名单——鉴权由外层 FastAPI 中间件保证。"""
    import server
    assert "/mcp" not in server.PUBLIC_PATHS


# --------------------------------------------------------------------------
# 端到端：官方 SDK 真客户端（协议可用性验收；执行期约束经协议层验证）
# --------------------------------------------------------------------------

async def _serve(app):
    """在独立线程的随机回环端口上起 uvicorn，返回 (server, port)。"""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0,
                            log_level="critical", lifespan="on")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(
        target=lambda: asyncio.run(uv_server.serve()), daemon=True
    )
    thread.start()
    for _ in range(100):
        if uv_server.started:
            break
        await asyncio.sleep(0.05)
    assert uv_server.started, "uvicorn 未能启动"
    port = uv_server.servers[0].sockets[0].getsockname()[1]
    return uv_server, port


@requires_mcp_sdk
@pytest.mark.asyncio
async def test_real_mcp_client_roundtrip():
    """真客户端四步：initialize → tools/list → tools/call → SSRF 拦截可见。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    registry = _test_registry()
    app = create_mcp_asgi_app(registry)
    uv_server, port = await _serve(app)

    try:
        async with streamable_http_client(f"http://127.0.0.1:{port}/") as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "research-agent"

                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert names == {"echo", "unsafe_fetch"}

                # 协议级 schema 一致：客户端看到的 = 注册表定义的
                echo_def = next(t for t in tools.tools if t.name == "echo")
                assert echo_def.input_schema == registry.get("echo").parameters

                result = await session.call_tool("echo", {"text": "你好，MCP"})
                payload = json.loads(result.content[0].text)
                assert payload["ok"] is True
                assert payload["data"]["echo"] == "你好，MCP"

                blocked = await session.call_tool(
                    "unsafe_fetch", {"url": "http://127.0.0.1:9/admin"}
                )
                payload = json.loads(blocked.content[0].text)
                assert payload["ok"] is False
                assert "安全策略拒绝" in payload["error"]

                # 缺失必填参数 → 注册表校验经协议层可见（统一返回结构）
                missing = await session.call_tool("echo", {})
                payload = json.loads(missing.content[0].text)
                assert payload["ok"] is False and "必填参数" in payload["error"]
    finally:
        uv_server.should_exit = True
