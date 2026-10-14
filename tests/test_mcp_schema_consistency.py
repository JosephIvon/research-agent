"""MCP schema 单一来源的约束级测试（实施草案 M5 · 复核 §3.4）。

存在的理由只有一个：**本文件不 import mcp，也不 importorskip**。MCP SDK 是可选
依赖，缺它时协议层跑不了，但"对外 schema 只来自 M3 注册表"这条约束照样能判红。
一旦这里也 importorskip，最需要守的漂移点（协议层抄了第二份 schema）恰好在没装
SDK 的机器和 CI 上毫无保护。

断言全部打在真实生产对象上（build_default_tools / build_mcp_registry），不在这里
重抄一份 schema 再互相比——那是自测自。真实 JSON-RPC 往返由 tests/test_mcp_server.py
的真客户端用例负责（有 SDK 才跑）。
"""
from src.agent.config import AGENT_WALL_CLOCK_BUDGET
from src.agent.tool_specs import build_default_tools, generate_research_report_tool
from src.agent.tools import Tool, ToolRegistry
from src.mcpserver import server as mcp_server

# MCP 对外暴露面（需求约定：web_search / fetch_site / generate_research_report 三个）。
MCP_EXPECTED_TOOLS = {"web_search", "fetch_site", "generate_research_report"}

# 刻意不对 MCP 暴露、但必须**显式**登记在这里的两个内部工具：它们读写 Runner 的
# workspace 运行期状态，而 MCP 是无状态单次调用，暴露了也没有可用的前置状态。
# 登记的意义是让"新增工具忘了同步暴露"必然变红，而不是悄悄溜过去。
INTERNAL_ONLY_TOOLS = {"extract_facts", "compare_competitors"}


async def _noop_handler():
    """占位 handler：只用于取一份共享 schema 做比对，从不执行。"""
    return None


def _sentinel(name: str) -> Tool:
    """特征明显的假 Tool：MCP 侧若自己声明 schema 而非引用注册表，
    build_mcp_registry() 就拿不到这个对象。"""

    async def _handler():
        return None

    return Tool(
        name=name,
        description=f"哨兵 {name}",
        parameters={
            "type": "object",
            "properties": {"sentinel": {"type": "string"}},
            "required": ["sentinel"],
        },
        handler=_handler,
        timeout=5.0,
    )


# --------------------------------------------------------------------------
# schema 单一来源（复核 §3.4：防漂移的约束级测试）
# --------------------------------------------------------------------------

def test_mcp_reuses_tool_objects_from_m3_registry(monkeypatch):
    """MCP 侧必须**直接复用** M3 注册表里的 Tool 对象。

    这里认对象身份而不是字段相等：字段相等挡不住"复制一份再慢慢漂移"——只要抄的
    那一刻还相等，测试就一直绿；而只要 MCP 侧不是从注册表取对象，拿到的必然是新
    对象，哨兵立刻对不上。
    """
    sentinel = ToolRegistry()
    for name in ("web_search", "fetch_site"):
        sentinel.add(_sentinel(name))
    monkeypatch.setattr(mcp_server, "build_default_tools", lambda llm, ws: sentinel)

    registry = mcp_server.build_mcp_registry()
    for name in ("web_search", "fetch_site"):
        assert registry.get(name) is sentinel.get(name), (
            f"{name} 未复用 M3 注册表的 Tool 对象——协议层不允许自抄一份 schema"
        )


def test_mcp_exposed_schemas_equal_m3_registry_fields():
    """对外暴露的描述与 schema 与 M3 注册表逐字段一致。

    跨实例只能比字段（build_default_tools 每调用一次都新建 Tool），对象级同一性
    由上面那条哨兵测试负责。
    """
    exposed_registry = mcp_server.build_mcp_registry()
    base = build_default_tools(None, {})

    for name in ("web_search", "fetch_site"):
        exposed = exposed_registry.get(name)
        source = base.get(name)
        assert exposed is not None and source is not None
        assert exposed.description == source.description, f"{name} 的描述已漂移"
        assert exposed.parameters == source.parameters, \
            f"{name} 的 schema 在 MCP 层出现第二份且已漂移"
        assert exposed.read_only is True


def test_generate_research_report_schema_comes_from_shared_spec():
    """generate_research_report 的 schema 住在 src/agent/tool_specs.py，
    协议层不得内联第二份：改共享定义必然带着 MCP 侧一起变。

    超时是 MCP 侧的执行策略（按墙钟预算放宽），一并钉住，免得重构时无声改语义。
    """
    mcp_tool = mcp_server.build_mcp_registry().get("generate_research_report")
    shared = generate_research_report_tool(_noop_handler, 1.0)

    assert mcp_tool.name == shared.name
    assert mcp_tool.description == shared.description
    assert mcp_tool.parameters is shared.parameters, \
        "generate_research_report 的 schema 出现了第二份（协议层内联了字面量）"
    assert mcp_tool.parameters.get("type") == "object"
    assert mcp_tool.timeout == max(300.0, AGENT_WALL_CLOCK_BUDGET * 2)
    assert mcp_tool.read_only is True


def test_mcp_exposes_exact_tool_set():
    """暴露面与方案文档 M5 一致，且全部只读。"""
    registry = mcp_server.build_mcp_registry()
    assert set(registry.names()) == MCP_EXPECTED_TOOLS
    for name in registry.names():
        assert registry.get(name).read_only is True


def test_new_default_tool_must_be_exposed_or_declared_internal():
    """单向约束：往 M3 注册表加工具，必须同步暴露到 MCP，否则显式登记为内部工具。

    generate_research_report 不进 ReAct 注册表（防模型递归调用报告生成），故不参与
    这个方向的比较。
    """
    source = set(build_default_tools(None, {}).names())
    unaccounted = source - MCP_EXPECTED_TOOLS - INTERNAL_ONLY_TOOLS
    assert not unaccounted, (
        f"注册表新增工具未在 MCP 侧暴露，也未登记为内部工具：{sorted(unaccounted)}"
    )