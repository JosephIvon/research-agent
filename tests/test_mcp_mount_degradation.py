"""MCP 挂载的优雅降级。

## 事故

`server.py` 的 `app.mount("/mcp", create_mcp_asgi_app())` 在**模块顶层**执行，
而 `create_mcp_asgi_app()` 在 SDK 缺失时 `raise RuntimeError`。后果：

    RuntimeError: mcp SDK 未安装（uv add 'mcp>=2.3'）
      → tests/test_tenant_isolation.py 等 5 个文件 collection error
      → **整个后端测试套件跑不起来**

CI 因为会 `uv sync --extra dev` 装上 mcp，所以那边不红——这正是它危险的地方：
破坏只在"依赖没装全"的真实环境里显形，而那种环境恰恰是本地开发机和
新克隆的仓库。

## 为什么降级而不是 fail-fast

MCP 是**可选的对外能力**（让外部 MCP 客户端调搜索/抓取/报告）。它不可用时，
主链路（报告生成、任务队列、引用校验）跟它毫无关系——为了让一个可选接口
缺席而让整个 API 起不来，代价不对等。

可用性没有因此被藏起来：`/health/ready` 的 `mcp` 段如实报告，
`/mcp` 本身返回 503 + 可执行的修复建议。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

# 动态推导仓库根：曾硬编码 Windows 绝对路径，Linux CI 上 FileNotFoundError
# （子进程 cwd 不存在），三个用例全红——教训：路径永远从 __file__ 推。
REPO = str(Path(__file__).resolve().parents[1])

# 子进程 stdout 在 Windows 上默认走本地编码（cp936），而 body 里含中文。
# 不强制 UTF-8 的话父进程解码会直接抛 UnicodeDecodeError——那是**测试的**故障，
# 不是被测代码的，会把红绿判反。
_CHILD_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

# -c 模式下 sys.path 是否含 cwd 存在版本差异，显式注入不赌默认行为
_PATH_SETUP = "import os, sys; sys.path.insert(0, os.getcwd())\n"


def _run_child(script):
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=REPO, env=_CHILD_ENV,
    )


def _force_sdk_missing():
    """在**子进程**里把 SDK 标记为缺失。

    为什么必须用子进程：`app.mount("/mcp", create_mcp_asgi_app())` 写在模块顶层，
    导入 `server` 的那一刻就已经执行完了。父进程里改 `_SDK_AVAILABLE` 只会影响
    之后新建的对象，已经挂上去的那个子应用不会变。
    """
    script = _PATH_SETUP + """
import src.mcpserver.server as m
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

m._SDK_AVAILABLE = False
app = m.create_mcp_asgi_app()
outer = Starlette(routes=[Mount("/mcp", app)])
c = TestClient(outer)
r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/list"})
print("STATUS", r.status_code)
print("BODY", r.text)
"""
    return _run_child(script)


def test_sdk_absence_must_not_raise():
    """最直接的判据：缺 SDK 时构造子应用不得抛异常。"""
    proc = _force_sdk_missing()
    assert "Traceback" not in proc.stderr, (
        f"缺 mcp SDK 时 create_mcp_asgi_app 抛了异常：\n{proc.stderr}"
    )
    assert "RuntimeError" not in proc.stderr, proc.stderr


def test_unavailable_mcp_returns_503_with_remedy():
    proc = _force_sdk_missing()
    assert "STATUS 503" in proc.stdout, f"期望 503，实际：\n{proc.stdout}\n{proc.stderr}"
    assert "mcp>=2.3" in proc.stdout, f"503 必须带可执行的修复建议：{proc.stdout}"
    assert "remedy" in proc.stdout


def test_importing_server_survives_a_missing_sdk():
    """回归的真实场景：本地没跑 uv sync 时，import server 必须仍然成功。

    否则 5 个测试文件直接 collection error，**任何验证都做不了**——
    包括后来者想验证自己的改动。
    """
    script = (
        _PATH_SETUP
        + "import src.mcpserver.server as m\n"
        "m._SDK_AVAILABLE = False\n"
        "import server\n"
        "print('IMPORT_OK')\n"
    )
    proc = _run_child(script)
    assert "IMPORT_OK" in proc.stdout, (
        f"缺 mcp SDK 导致 import server 失败：\n{proc.stderr[-2000:]}"
    )


def test_health_stays_healthy_without_mcp():
    """可选能力缺席**不得**让存活探针变红——否则探针天天假红，
    真正的挂掉反而被淹没。"""
    import server

    assert server.health()["status"] == "healthy"


def test_ready_reports_mcp_availability_without_failing():
    """就绪判定仍只看 LLM：MCP 缺席时 status 必须还是 ready，
    但 mcp 段必须如实说不可用。"""
    import json as _json

    import server

    from src.config import settings as st

    original = st.MINIMAX_API_KEY
    st.MINIMAX_API_KEY = "a" * 40
    try:
        response = server.health_ready()
    finally:
        st.MINIMAX_API_KEY = original

    payload = _json.loads(response.body) if hasattr(response, "body") else response
    assert payload["status"] == "ready", "MCP 缺席不应把就绪判定改成 not_ready"
    assert "mcp" in payload, f"必须报告 MCP 状态：{payload}"
    assert "available" in payload["mcp"]


def test_health_never_leaks_mcp_internals():
    """`/health` 只暴露一行摘要，不要把 SDK 细节抖给公开探针。"""
    import server

    blob = repr(server.health())
    assert "mcpserver" not in blob
    assert "StreamableHTTP" not in blob