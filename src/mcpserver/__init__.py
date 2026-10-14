"""MCP Server 层（M5）：把 Agent 工具注册表暴露为标准 MCP 协议。

包名刻意叫 `mcpserver` 而不是 `mcp`：后者会遮蔽官方 SDK 的 `mcp` 包
（同根目录下 `import mcp` 会解析到本仓库），实测踩过。

设计决策（实施草案 M5 + 复核 §3.4）：
- **单一 server，挂载进现有 FastAPI**（server.py 一行 `app.mount("/mcp", ...)`）：
  复用 JWT/限流/TrustedHost 中间件；`/mcp` 不进 PUBLIC_PATHS——鉴权由外层保证，
  不另起进程重写一套鉴权。
- **schema 单一来源**：`tools/list` 直接由 ToolRegistry 的 JSON Schema 生成，
  MCP 层禁止手抄第二份 schema；`test_mcp_server.py` 锁死两者一致，漂移即 CI 红。
- **无状态 request/response**：每个 tools/call 独立执行（stateless 模式），
  不维护跨请求会话——搜索/抓取/报告天然是请求-响应形态。
- **执行零旁路**：所有调用一律走 `registry.execute`——只读白名单、入参校验、
  URL 二次 SSRF 校验、超时、统一返回 `{ok, data, error, elapsed}` 全部继承。
"""
from src.mcpserver.server import (
    MCP_UNAVAILABLE_REMEDY,
    build_mcp_registry,
    create_mcp_asgi_app,
    create_mcp_server,
    mcp_sdk_available,
    mcp_tool_definitions,
    mount_mcp,
)

__all__ = [
    "MCP_UNAVAILABLE_REMEDY",
    "build_mcp_registry",
    "create_mcp_server",
    "create_mcp_asgi_app",
    "mcp_sdk_available",
    "mcp_tool_definitions",
    "mount_mcp",
]
