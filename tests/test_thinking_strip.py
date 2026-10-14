"""`<think>` 推理块剥离的测试。

## 为什么必须有这个模块

真实 MiniMax 冒烟暴露的问题：M3 类推理模型把思考过程内联在 `content` 里返回，
全仓**没有任何一处**剥离它（`grep -i "think|reasoning"` 零命中）。后果有两层：

1. `extract_key_info` 的 `_parse_json_response` 拿到的是以 `<think>` 开头的文本，
   `json.loads` 必然抛 `Expecting value: line 1 column 1`，于是 **100% 降级**到
   `content_summary`——结构化抽取整条链路静默失效，还不报错。
2. 剥离前实测推理内容占输出 **66.6%**（5894 字里 3927 字）。它会被当成报告正文
   交付给用户，还会把模型自己的猜测当成事实主张送进引用校验。

接线测试一律打在**生产客户端类**上（用桩替换传输层），断言跑的是真实返回路径。
"""
from types import SimpleNamespace

import asyncio

import pytest

from src.agent.citations import EvidenceStore
from src.llm import LLMError, OpenAICompatibleClient, strip_thinking
from src.workflow.mvp_workflow import CopywriterAgent, CoordinatorAgent, _parse_json_response

EXTRACTED = [
    {"key": "pricing", "value": "标准版 49 美元/席位/月。", "source_url": "https://acme.test/pricing"},
]

# --------------------------------------------------------------------------
# 传输层桩：只替换网络出口，其余走真实客户端代码
# --------------------------------------------------------------------------

_counter = 0


def _make_client(response):
    """构造一个传输层被桩替换的 OpenAICompatibleClient。"""
    global _counter
    _counter += 1

    class _Completions:
        def __init__(self):
            self.calls = []

        def create(self, **params):
            self.calls.append(params)
            return response

    client = OpenAICompatibleClient(
        api_key="stub-key",
        base_url="http://127.0.0.1:1/v1",
        default_model="stub-model",
        provider_name=f"stub-{_counter}",
    )
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions())
    )
    return client


def _response(content, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=None,
    )


# --------------------------------------------------------------------------
# 纯函数行为
# --------------------------------------------------------------------------

def test_complete_think_block_is_removed():
    assert strip_thinking("<think>先分析定价</think>最终答案是 49 美元") == "最终答案是 49 美元"


def test_think_is_removed_when_it_wraps_the_whole_answer():
    text = "<think>\n用户问定价\n</think>\n\n## 结论\n定价 49 美元"
    assert strip_thinking(text) == "## 结论\n定价 49 美元"


def test_unclosed_think_block_yields_no_answer():
    """输出被 max_tokens 截断时，推理块没有闭合——其后全是思考，没有正文可言。

    必须返回空串而不是把推理当正文交出去。
    """
    assert strip_thinking("<think>还在想…（被截断）") == ""


def test_multiple_think_blocks_are_all_removed():
    assert strip_thinking("<think>A</think>正文1<think>B</think>正文2") == "正文1正文2"


def test_think_tags_are_case_insensitive():
    assert strip_thinking("<THINK>a</THINK>答案") == "答案"
    assert strip_thinking("<Think>a</Think>答案") == "答案"


def test_stray_closing_tag_is_cleaned():
    """模型偶尔吐出孤立闭合标签，不能让裸标签混进交付内容。"""
    assert strip_thinking("<think>a</think>正文</think>") == "正文"


def test_text_without_think_is_returned_unchanged():
    plain = "## 结论\n定价 49 美元 [e:abc123def456]。"
    assert strip_thinking(plain) == plain


def test_strip_thinking_is_idempotent():
    once = strip_thinking("<think>x</think>答案")
    assert strip_thinking(once) == once


def test_empty_and_none_input():
    assert strip_thinking("") == ""
    assert strip_thinking(None) == ""


# --------------------------------------------------------------------------
# 接线：chat_completion
# --------------------------------------------------------------------------

def test_chat_completion_returns_content_without_think():
    client = _make_client(_response("<think>推理</think>正文：定价 49 美元"))
    assert client.chat_completion([{"role": "user", "content": "定价?"}]) == "正文：定价 49 美元"


def test_chat_completion_returns_none_when_output_was_only_thinking():
    client = _make_client(_response("<think>想了半天，答案来不及输出"))
    assert client.chat_completion([{"role": "user", "content": "x"}]) is None


# --------------------------------------------------------------------------
# 接线：chat_completion_raw（ReAct 走这条路径）
# --------------------------------------------------------------------------

def test_chat_completion_raw_strips_think_from_content():
    client = _make_client(_response("<think>规划</think>我要调用工具"))
    raw = client.chat_completion_raw([{"role": "user", "content": "x"}])
    assert raw["content"] == "我要调用工具"
    assert raw["tool_calls"] == []


def test_chat_completion_raw_keeps_tool_calls_while_stripping_content():
    call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="search", arguments='{"q":"acme"}'),
    )
    client = _make_client(
        _response("<think>先搜</think>", tool_calls=[call], finish_reason="tool_calls")
    )
    raw = client.chat_completion_raw([{"role": "user", "content": "x"}], tools=[{"name": "search"}])
    assert raw["content"] is None, "只剩推理时 content 必须是 None，不能是空串"
    assert len(raw["tool_calls"]) == 1
    assert raw["tool_calls"][0]["name"] == "search"
    assert raw["finish_reason"] == "tool_calls"


# --------------------------------------------------------------------------
# 接线：stream_completion
# --------------------------------------------------------------------------

def test_stream_completion_accumulates_then_strips():
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])
        for part in ("<think>", "推理", "</think>", "正", "文")
    ]
    client = _make_client(chunks)
    assert client.stream_completion([{"role": "user", "content": "x"}]) == "正文"


# --------------------------------------------------------------------------
# 接线：结构化抽取不再 100% 降级
# --------------------------------------------------------------------------

THINK_PREFIXED_JSON = (
    "<think>用户给了定价页面，先找价格。</think>\n"
    '```json\n[{"key": "pricing", "value": "标准版 49 美元/席位/月"}]\n```'
)


def test_extract_key_info_parses_json_behind_a_think_block():
    """这是本次缺陷的真实后果：think 前缀让 JSON 解析必然失败 → 抽取整条链路降级。

    断言的不是"解析成功"，而是**没有降级**：返回的 key 是模型给的 pricing，
    而不是兜底路径的 content_summary。
    """
    client = _make_client(_response(THINK_PREFIXED_JSON))

    agent = CoordinatorAgent.__new__(CoordinatorAgent)
    agent.llm_client = client

    import asyncio

    items = asyncio.run(
        agent.extract_key_info("页面里写着标准版 49 美元/席位/月", "https://acme.test/pricing")
    )

    keys = {i["key"] for i in items}
    assert keys == {"pricing"}, f"不该降级，实际拿到 {keys}"
    assert items[0]["source_url"] == "https://acme.test/pricing"


def test_parse_json_response_itself_tolerates_think_prefix():
    """第二道防线：即便将来换了别的 LLM 客户端实现、绕过了客户端层剥离，
    JSON 解析入口也不能被推理前缀打穿（打穿 = 静默降级 + 丢失全部结构化数据）。"""
    assert _parse_json_response(THINK_PREFIXED_JSON)[0]["key"] == "pricing"


def test_parse_json_response_still_rejects_garbage():
    """防线不能变成橡皮擦：非 JSON 输入必须照常抛错。"""
    with pytest.raises(Exception):
        _parse_json_response("<think>x</think>这完全不是 JSON")


def test_parse_json_response_rejects_thinking_only_output():
    """只有推理、没有正文时必须抛错，不能返回空列表蒙混过关。

    返回 `[]` 会让 `extract_key_info` 交出空的 extracted_data——后面整条链路
    没有证据可引用，报告只能靠编，而日志上什么都看不出来。
    """
    with pytest.raises(ValueError):
        _parse_json_response("<think>还在想…（输出被截断）")


def test_extract_key_info_falls_back_when_output_was_only_thinking():
    """被截断的推理输出必须落到 content_summary 兜底，且原文不能丢。"""
    client = _make_client(_response("<think>还在想…"))

    agent = CoordinatorAgent.__new__(CoordinatorAgent)
    agent.llm_client = client

    items = asyncio.run(
        agent.extract_key_info("页面里写着标准版 49 美元/席位/月", "https://acme.test/pricing")
    )

    assert [i["key"] for i in items] == ["content_summary"]
    assert "49" in items[0]["value"], "兜底路径必须保住页面原文，否则报告无据可依"
    assert items[0]["source_url"] == "https://acme.test/pricing"


def test_generate_report_raises_instead_of_shipping_an_empty_report():
    """只有推理、没有正文时必须显式失败。

    这条防线不可省：空报告会被 `verify_report` **平凡判为全部通过**
    （claims=[] → coverage=1.0 / grounding=1.0 / 无幻觉 URL），于是一份空文档
    会被打上"引用校验通过 / 可信"的标签直接交付——静默失败比报错危险得多。

    真实冒烟确实撞上过：同一个报告提示词，一次正常出 2431 字正文，
    一次 think 吃满 4096 max_tokens 导致正文为空。
    """
    client = _make_client(_response("<think>还在想…（被 max_tokens 截断）"))
    writer = CopywriterAgent.__new__(CopywriterAgent)
    writer.llm_client = client

    with pytest.raises(LLMError):
        asyncio.run(writer.generate_report(
            "企业级大模型网关选型对比", EXTRACTED, {},
            evidence_store=EvidenceStore.from_extracted_data(EXTRACTED),
        ))


def test_generate_report_still_returns_body_when_thinking_is_stripped():
    """负向对照：有正文就必须正常返回，不能被防御逻辑误伤。"""
    client = _make_client(_response("<think>分析</think>## 结论\n定价 49 美元"))
    writer = CopywriterAgent.__new__(CopywriterAgent)
    writer.llm_client = client

    report = asyncio.run(writer.generate_report(
        "企业级大模型网关选型对比", EXTRACTED, {},
        evidence_store=EvidenceStore.from_extracted_data(EXTRACTED),
    ))

    assert "定价 49 美元" in report