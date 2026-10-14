"""ScriptedLLMClient（"fake" provider，WP-1/M1）的单元与全流程接线测试。

两个使命：
1. 单元层：意图识别（显式标记 + legacy 特征句兜底）、步进/尾重复、tool_call 结构、
   脚本来源（构造参数 > LLM_FAKE_SCRIPT 文件 > 内置缺省）、构造期校验、异步包装。
2. 接线层：`LLM_PROVIDER=fake` 下**完整 legacy 工作流**零网络、零密钥跑通——
   这是 test_citation_wiring.py 刻意不碰的部分（那边只测接线，不跑 run()）。

零网络护栏：monkeypatch `socket.socket.connect`，任何对外连接直接把测试炸红。
"""
import asyncio
import json
import socket

import pytest

from src.llm import (
    ScriptedLLMClient,
    detect_llm_intent,
    get_llm_client,
    list_available_providers,
)


def _msgs(system: str, user: str = "内容"):
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# --------------------------------------------------------------------------
# 注册与选择
# --------------------------------------------------------------------------

def test_fake_provider_is_registered_and_selectable():
    assert "fake" in list_available_providers()
    client = get_llm_client("fake")
    assert isinstance(client, ScriptedLLMClient)


# --------------------------------------------------------------------------
# 意图识别
# --------------------------------------------------------------------------

def test_intent_marker_takes_priority_over_legacy_hints():
    messages = _msgs("你是一个任务分解专家。<!--intent:custom_step-->")
    assert detect_llm_intent(messages) == "custom_step"


@pytest.mark.parametrize("hint,intent", [
    ("你是一个任务分解专家。请将用户的调研查询分解", "decompose"),
    ("你是一个信息提取专家。请从以下网页内容中提取关键信息", "extract"),
    ("你是一名专业的市场调研分析师，擅长撰写结构化的竞品分析报告", "report"),
    ("你是一名资深产品经理，擅长将竞品分析转化为PRD文档", "prd"),
])
def test_legacy_prompts_are_recognized_without_modification(hint, intent):
    """legacy 提示词一个字都不用改就能被脚本化——这是 fake 能跑通存量流程的前提。"""
    assert detect_llm_intent(_msgs(hint)) == intent


def test_no_intent_returns_none():
    assert detect_llm_intent(_msgs("无关提示词")) is None


# --------------------------------------------------------------------------
# 脚本语义
# --------------------------------------------------------------------------

def test_steps_advance_then_repeat_last_entry():
    client = ScriptedLLMClient(script={"extract": ["第一页", "第二页"]})
    results = [client.chat_completion(_msgs("信息提取专家")) for _ in range(3)]
    assert results == ["第一页", "第二页", "第二页"]


def test_tool_call_entry_is_returned_as_dict():
    entry = {"type": "tool_call", "name": "web_search", "arguments": '{"q": "acme"}'}
    client = ScriptedLLMClient(script={"agent_step": [entry]})
    assert client.chat_completion(_msgs("<!--intent:agent_step-->")) == entry


def test_calls_record_intent_step_messages_and_tools():
    client = ScriptedLLMClient(script={"extract": ["a", "b"]})
    client.chat_completion(_msgs("信息提取专家"), tools=[{"name": "t"}])
    client.chat_completion(_msgs("信息提取专家"))
    assert [c["step"] for c in client.calls] == [0, 1]
    assert client.calls[0]["tools"] == [{"name": "t"}]
    assert "信息提取专家" in client.calls[0]["messages"][0]["content"]


def test_unknown_intent_fails_loud_with_available_intents():
    client = ScriptedLLMClient(script={"extract": ["a"]})
    with pytest.raises(ValueError, match="decompose, extract, prd, report"):
        client.chat_completion(_msgs("完全未知的提示词"))
    with pytest.raises(ValueError, match="custom"):
        client.chat_completion(_msgs("<!--intent:custom-->"))


def test_invalid_script_entry_is_rejected_at_construction():
    with pytest.raises(ValueError, match="extract"):
        ScriptedLLMClient(script={"extract": [123]})
    with pytest.raises(ValueError, match="非空列表"):
        ScriptedLLMClient(script={"extract": []})


def test_script_from_env_file_overrides_only_listed_intents(tmp_path, monkeypatch):
    script_file = tmp_path / "script.json"
    script_file.write_text(
        json.dumps({"decompose": ['[{"id": "9"}]']}, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setenv("LLM_FAKE_SCRIPT", str(script_file))
    client = ScriptedLLMClient()

    assert client.chat_completion(_msgs("任务分解专家")) == '[{"id": "9"}]'
    # 未覆盖的意图回落到内置缺省，而不是整体失效
    assert isinstance(client.chat_completion(_msgs("市场调研分析师")), str)


def test_stream_completion_serializes_tool_call_to_json():
    entry = {"type": "tool_call", "name": "web_search", "arguments": "{}"}
    client = ScriptedLLMClient(script={"agent_step": [entry]})
    text = client.stream_completion(_msgs("<!--intent:agent_step-->"))
    assert json.loads(text) == entry


def test_async_wrapper_returns_scripted_response():
    client = ScriptedLLMClient(script={"extract": ["异步结果"]})
    result = asyncio.run(client.achat_completion(_msgs("信息提取专家")))
    assert result == "异步结果"


def test_env_script_must_be_a_json_object(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text('["not", "an", "object"]', encoding="utf-8")
    monkeypatch.setenv("LLM_FAKE_SCRIPT", str(bad))
    with pytest.raises(ValueError, match="JSON 对象"):
        ScriptedLLMClient()


# --------------------------------------------------------------------------
# 全流程：LLM_PROVIDER=fake 跑通完整 legacy 工作流，零网络零密钥
# --------------------------------------------------------------------------

EXTRACT_ITEMS = [
    {"key": "pricing", "value": "定价：标准版 $49/席位/月，企业版 $99/席位/月。", "confidence": 0.9},
    {"key": "features", "value": ["多模型路由", "团队空间", "审计日志"], "confidence": 0.9},
    {"key": "models", "value": ["chat-pro", "chat-lite"], "confidence": 0.9},
    {"key": "target_users", "value": "目标用户：中小团队与独立开发者", "confidence": 0.9},
    {"key": "integrations", "value": "集成生态：飞书、GitHub、Jira", "confidence": 0.9},
    {"key": "limitations", "value": "限制：无私有化部署，审计日志仅企业版可用", "confidence": 0.9},
    {"key": "reviews", "value": "用户评价：口碑整体积极，评测反馈良好", "confidence": 0.9},
]

TARGET_URL = "https://acme.example.com/pricing"


class _FakeWebCrawler:
    """离线替身：URL 安全校验与抓取本身各有测试，这里只隔离网络。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetch_site_profile(self, url, **kwargs):
        return {
            "content": "Acme 官网内容占位",
            "pages": [{"url": url, "content": "Acme 官网内容占位"}],
            "discovered_urls": [],
        }


def test_full_legacy_workflow_runs_offline_with_fake_llm(tmp_path, monkeypatch):
    from src.agent.citations import EvidenceStore
    from src.storage import database as dbmod
    from src.workflow import mvp_workflow

    # 预计算证据 id：工作流对脚本的归一化结果必须与这里构造的完全一致
    expected_extracted = [dict(item, source_url=TARGET_URL) for item in EXTRACT_ITEMS]
    store = EvidenceStore.from_extracted_data(expected_extracted)
    pricing_cite = store.evidences[0].citation

    report_md = (
        "## 定价\n\n"
        f"标准版 $49/席位/月 {pricing_cite}，企业版 $99/席位/月 {pricing_cite}。\n"
    )
    client = ScriptedLLMClient(script={
        "extract": [json.dumps(EXTRACT_ITEMS, ensure_ascii=False)],
        "report": [report_md],
    })

    # 零网络护栏：fake 模式下任何对外连接都是测试失败。
    # 回环必须放行：Windows 上 asyncio 用 127.0.0.1 自连管模拟 socketpair，
    # 一刀切会杀死事件循环自身。
    real_connect = socket.socket.connect

    def _guard_connect(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in ("127.0.0.1", "::1", "localhost", ""):
            return real_connect(self, address)
        raise AssertionError(f"fake 模式下不允许对外网络连接: {address}")

    monkeypatch.setattr(socket.socket, "connect", _guard_connect)

    monkeypatch.setattr(mvp_workflow, "WebCrawler", _FakeWebCrawler)
    monkeypatch.setattr(mvp_workflow, "get_llm_client", lambda *a, **k: client)
    # URL 校验走透传以保持离线；其真实行为由 test_security_baseline 覆盖
    monkeypatch.setattr(mvp_workflow, "validate_fetch_url", lambda u: u)
    monkeypatch.setattr(mvp_workflow, "validate_fetch_urls", lambda us: list(us))

    monkeypatch.setattr("src.config.settings.OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)

    workflow = mvp_workflow.MVPWorkflow()
    state = asyncio.run(workflow.run(
        "Acme 定价调研",
        target_urls=[TARGET_URL],
        enable_search=False,
        deliverables={"report": True, "prd": False},
        task_id="fake-e2e-0001",
    ))

    assert state["status"] == "completed", state.get("termination_reason")
    assert state["citation_trusted"] is True, state.get("citation_violations")
    assert pricing_cite in state["report_final"]

    intents = [call["intent"] for call in client.calls]
    assert intents[0] == "decompose", "流程必须先做任务分解"
    assert intents.count("extract") == 1
    assert "report" in intents

    # 报告确实落盘
    out_dir = tmp_path / "output"
    files = list(out_dir.glob("research_report_*.md"))
    assert len(files) == 1 and pricing_cite in files[0].read_text(encoding="utf-8")


def test_machine_appendix_is_exempt_from_citation_checks():
    """回归：数据完整度附录由程序生成，其数字（5.6/10、进度条、条数）不得被
    引用校验当作模型主张——否则任何带附录的全绿报告都会被误标低可信。
    该缺陷正是由 fake 全流程测试首次抓到的。
    """
    from src.agent.citations import EvidenceStore, verify_report
    from src.tools.quality_scorer import QualityScorer

    expected = [dict(item, source_url=TARGET_URL) for item in EXTRACT_ITEMS]
    store = EvidenceStore.from_extracted_data(expected)
    cite = store.evidences[0].citation

    appendix = QualityScorer([]).generate_quality_section()
    assert "<!--machine-appendix-->" in appendix, "附录必须自带豁免标记"

    report = f"标准版 $49/席位/月 {cite}。\n\n{appendix}"
    result = verify_report(report, store)
    assert result["passed"] is True, result["violations"]
    assert result["claims_checked"] == 1, "附录里的数字句不应进入主张统计"
