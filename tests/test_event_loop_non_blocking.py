"""事件循环阻塞回归测试。

背景：LLM 客户端是同步实现（阻塞式网络 I/O + `time.sleep` 重试退避），
过去直接在 async 工作流里调用。单次退避最长 LLM_RETRY_BACKOFF_MAX，乘以
重试次数可达分钟级，期间整个事件循环被冻结——单 worker 部署下，SSE 心跳和
其他并发请求会一起停摆。

修复方式是把同步调用丢进线程池（`LLMClient.achat_completion` /
`asyncio.to_thread`）。本文件用一个"心跳协程"直接测量事件循环是否仍可调度：
如果 LLM 调用仍在事件循环上同步执行，心跳计数会停在调用开始前的水平。
"""
import asyncio
import threading
import time

import pytest

from src.llm import LLMClient
from src.workflow.mvp_workflow import CoordinatorAgent, CopywriterAgent

# 模拟一次 LLM 往返的耗时。取 0.4s 是为了在 CI 抖动下仍有充足余量：
# 事件循环空闲时心跳约能跑 40 次，被冻住则约 0 次。
LLM_LATENCY = 0.4
HEARTBEAT_INTERVAL = 0.01
MIN_EXPECTED_TICKS = 8


@pytest.fixture(autouse=True)
def _isolate_search_tool_singleton():
    """隔离模块级单例。

    `CoordinatorAgent.__init__` 会调用 `get_search_tool()`，把 `_search_tool_instance`
    实例化。`tests/test_security_baseline.py` 断言该单例为 None，而本文件的名字在
    它之前执行——不还原就会污染后续用例。测试之间不应共享这种隐式状态。
    """
    from src.tools import search_tool as search_module

    before = search_module._search_tool_instance
    try:
        yield
    finally:
        search_module._search_tool_instance = before


class SlowLLM(LLMClient):
    """模拟同步 LLM 客户端：用阻塞式 sleep 代替网络 I/O。

    必须继承 LLMClient：`achat_completion` 定义在基类上，工作流按该契约调用。
    生产路径 `get_llm_client()` 返回的也都是 LLMClient 子类。
    """

    def __init__(self, latency: float = LLM_LATENCY, response: str = "# 报告"):
        self.latency = latency
        self.response = response
        self.calls = 0

    def chat_completion(self, messages, **kwargs):
        self.calls += 1
        time.sleep(self.latency)
        return self.response

    def stream_completion(self, messages, **kwargs):
        # 报告生成走流式（见 mvp_workflow.generate_report）：必须同样阻塞+计数，
        # 否则"生成报告不阻塞事件循环"这条断言会退化成"根本没调用"
        self.calls += 1
        time.sleep(self.latency)
        return self.response

    def extract_structured_data(self, content, schema):  # pragma: no cover - 未使用
        return {}


async def _ticks_while_running(operation) -> int:
    """在执行 operation 期间，让心跳协程自由跑，返回心跳次数。"""
    ticks = 0
    stop = False

    async def heartbeat():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            ticks += 1

    task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)  # 先让心跳跑起来
    try:
        await operation()
    finally:
        stop = True
        await task
    return ticks


class TestWorkflowDoesNotBlockEventLoop:
    @pytest.mark.asyncio
    async def test_decompose_task_does_not_block_event_loop(self):
        llm = SlowLLM(response='[{"id": "1", "description": "分析", "priority": 1}]')
        agent = CoordinatorAgent(llm)

        ticks = await _ticks_while_running(
            lambda: agent.decompose_task("调研问题", [])
        )

        assert llm.calls == 1, "LLM 应被调用一次"
        assert ticks >= MIN_EXPECTED_TICKS, (
            f"LLM 调用期间事件循环被冻结，心跳仅 {ticks} 次，"
            "说明同步 chat_completion 仍在事件循环上执行"
        )

    @pytest.mark.asyncio
    async def test_extract_key_info_does_not_block_event_loop(self):
        llm = SlowLLM(
            response='[{"key": "pricing", "value": "免费额度 1000 次/月", "confidence": 0.9}]'
        )
        agent = CoordinatorAgent(llm)

        ticks = await _ticks_while_running(
            lambda: agent.extract_key_info("页面内容", "https://example.com")
        )

        assert llm.calls == 1
        assert ticks >= MIN_EXPECTED_TICKS, (
            f"extract_key_info 阻塞了事件循环，心跳仅 {ticks} 次"
        )

    @pytest.mark.asyncio
    async def test_generate_report_does_not_block_event_loop(self):
        llm = SlowLLM(response="# 竞品分析报告")
        writer = CopywriterAgent(llm)

        ticks = await _ticks_while_running(
            lambda: writer.generate_report(
                "调研问题",
                [{"key": "pricing", "value": "$10/月", "source_url": "https://example.com"}],
                {"score": 8.0, "warnings": [], "passed": True},
            )
        )

        assert llm.calls == 1
        assert ticks >= MIN_EXPECTED_TICKS, (
            f"generate_report 阻塞了事件循环，心跳仅 {ticks} 次"
        )


class TestAsyncWrapperPrimitive:
    def test_achat_completion_returns_result(self):
        class _Client(LLMClient):
            def __init__(self):
                self.received = None

            def chat_completion(self, messages, **kwargs):
                self.received = messages
                return "ok"

            def stream_completion(self, messages, **kwargs):
                return "ok"

            def extract_structured_data(self, content, schema):
                return {}

        client = _Client()
        result = asyncio.run(client.achat_completion([{"role": "user", "content": "hi"}]))

        assert result == "ok"
        assert client.received == [{"role": "user", "content": "hi"}]

    @pytest.mark.asyncio
    async def test_achat_completion_runs_in_a_separate_thread(self):
        main_thread = threading.current_thread().name
        seen = {}

        class _Client(LLMClient):
            def chat_completion(self, messages, **kwargs):
                seen["thread"] = threading.current_thread().name
                time.sleep(0.1)
                return "ok"

            def stream_completion(self, messages, **kwargs):
                return "ok"

            def extract_structured_data(self, content, schema):
                return {}

        ticks = await _ticks_while_running(
            lambda: _Client().achat_completion([{"role": "user", "content": "hi"}])
        )

        assert seen["thread"] != main_thread, "同步调用未离开事件循环线程"
        assert ticks >= MIN_EXPECTED_TICKS, f"心跳仅 {ticks} 次，事件循环仍被冻结"