"""usage 真值贯通测试（M6：tiktoken 估 + usage 真值）。

背景：provider 返回的 prompt/completion tokens 是唯一真值，字符估算只在 usage
缺失时兜底。但改造前只有 `chat_completion_raw` 读得到 usage——`chat_completion`
（只回正文或首个 tool_call）与 `stream_completion`（回纯字符串）都把它整个丢掉，
于是除 ReAct 外的所有调用路径永远只有估算，成本账目带着 ±30% 的误差。

两条补齐路径的共同契约：
- 返回类型逐字不变（它们被全仓按 str | dict 使用），只加**可选**的 usage_sink；
- 不传 usage_sink 时连 `response.usage` 都不许碰——下面的 `_UsageBombResponse`
  就是这条零开销契约的可执行断言。

传输层一律用桩替换：不产生网络调用，不读 API key，不计费。
"""
import asyncio
from types import SimpleNamespace

from src.llm import (
    OpenAICompatibleClient,
    ScriptedLLMClient,
    _extract_usage,
    _notify_usage,
    _stream_usage_default,
)

_counter = 0


def _make_client(response):
    """传输层被桩替换的 OpenAICompatibleClient。provider 名唯一，避免熔断器串扰。"""
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
        provider_name=f"usage-stub-{_counter}",
    )
    client.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    return client


def _usage(prompt_tokens, completion_tokens):
    return SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


def _response(content, usage=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


def _tool_call(name="web_search", arguments='{"q":"acme"}'):
    return SimpleNamespace(id="call_1", function=SimpleNamespace(name=name, arguments=arguments))


def _chunks(*parts, final_usage=None):
    """流式 chunk 序列：正文若干片 + 可选的收尾 usage 块（choices 为空）。"""
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])
        for part in parts
    ]
    if final_usage is not None:
        chunks.append(SimpleNamespace(choices=[], usage=final_usage))
    return chunks


def _msgs(intent):
    return [
        {"role": "system", "content": f"<!--intent:{intent}-->"},
        {"role": "user", "content": "x"},
    ]


class _UsageBombResponse:
    """读 `usage` 就爆炸的响应对象。

    零开销不能只靠"我们保证不看"这种承诺，得让它可执行：不传 usage_sink 的路径
    一旦触碰 usage，本类立刻把测试打红。
    """

    def __init__(self, choices):
        self.choices = choices

    @property
    def usage(self):
        raise AssertionError("未传 usage_sink 时不得读取 usage（零开销红线）")


def _bomb_response(content="正文"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return _UsageBombResponse([SimpleNamespace(message=message, finish_reason="stop")])


def test_extract_usage_handles_missing_attribute_and_bogus_types():
    assert _extract_usage(SimpleNamespace()) == {
        "prompt_tokens": None, "completion_tokens": None}
    assert _extract_usage(_response("x", usage=None)) == {
        "prompt_tokens": None, "completion_tokens": None}
    # provider 偶尔回字符串/布尔：宁可当缺失（None，交调用方估算），也不能把
    # True 当成 1 个 token 记进账里
    assert _extract_usage(
        SimpleNamespace(usage=SimpleNamespace(prompt_tokens="120", completion_tokens=True))
    ) == {"prompt_tokens": None, "completion_tokens": None}
    assert _extract_usage({"usage": {"prompt_tokens": 12, "completion_tokens": 3}}) == {
        "prompt_tokens": 12, "completion_tokens": 3}


def test_notify_usage_swallows_sink_errors_and_skips_none_sink():
    def _boom(_usage_dict):
        raise RuntimeError("记账炸了")

    _notify_usage(_boom, {"prompt_tokens": 1, "completion_tokens": 1})  # 不抛
    _notify_usage(None, {"prompt_tokens": 1, "completion_tokens": 1})  # 不抛


def test_stream_usage_default_switch_is_off_and_env_driven(monkeypatch):
    monkeypatch.delenv("LLM_STREAM_INCLUDE_USAGE", raising=False)
    assert _stream_usage_default() is False, "默认必须关：不支持的后端会 400"
    monkeypatch.setenv("LLM_STREAM_INCLUDE_USAGE", "true")
    assert _stream_usage_default() is True
    monkeypatch.setenv("LLM_STREAM_INCLUDE_USAGE", "OFF")
    assert _stream_usage_default() is False


def test_chat_completion_sink_receives_real_usage_and_return_type_unchanged():
    client = _make_client(_response("正文：定价 49 美元", usage=_usage(123, 45)))
    seen = []

    result = client.chat_completion([{"role": "user", "content": "定价?"}], usage_sink=seen.append)

    assert result == "正文：定价 49 美元", "返回值类型/内容不得因 usage_sink 改变"
    assert seen == [{"prompt_tokens": 123, "completion_tokens": 45}]


def test_chat_completion_without_sink_never_touches_usage():
    """零开销红线：不传 usage_sink 就连 response.usage 都不许读。"""
    client = _make_client(_bomb_response("正文"))
    assert client.chat_completion([{"role": "user", "content": "x"}]) == "正文"


def test_chat_completion_sink_fires_on_tool_call_path_too():
    client = _make_client(
        _response(None, usage=_usage(88, 12), tool_calls=[_tool_call()], finish_reason="tool_calls")
    )
    seen = []

    result = client.chat_completion(
        [{"role": "user", "content": "x"}], tools=[{"name": "web_search"}], usage_sink=seen.append)

    # 首个 tool_call 的 dict 形态是既有契约，不能因为加了回调而变形
    assert result == {"type": "tool_call", "name": "web_search", "arguments": '{"q":"acme"}'}
    assert seen == [{"prompt_tokens": 88, "completion_tokens": 12}]


def test_chat_completion_sink_receives_none_usage_when_provider_omits_it():
    client = _make_client(_response("正文"))
    seen = []

    client.chat_completion([{"role": "user", "content": "x"}], usage_sink=seen.append)

    assert seen == [{"prompt_tokens": None, "completion_tokens": None}], \
        "provider 没给 usage 也必须回调：调用方据此决定是否走估算，不能靠'没回调'区分"


def test_chat_completion_sink_failure_does_not_break_the_call():
    def _boom(_usage_dict):
        raise RuntimeError("记账炸了")

    client = _make_client(_response("正文", usage=_usage(5, 5)))

    assert client.chat_completion([{"role": "user", "content": "x"}], usage_sink=_boom) == "正文"


def test_chat_completion_sink_called_once_per_request():
    client = _make_client(_response("正文", usage=_usage(5, 6)))
    seen = []

    client.chat_completion([{"role": "user", "content": "x"}], usage_sink=seen.append)

    assert len(seen) == 1, "一次请求只能记一笔，重复回调会双倍计费"


def test_chat_completion_raw_usage_contract_unchanged():
    client = _make_client(_response("正文", usage=_usage(10, 20)))
    assert client.chat_completion_raw([{"role": "user", "content": "x"}])["usage"] == {
        "prompt_tokens": 10, "completion_tokens": 20}

    no_usage = _make_client(_response("正文", usage=None))
    assert no_usage.chat_completion_raw([{"role": "user", "content": "x"}])["usage"] == {
        "prompt_tokens": None, "completion_tokens": None}


def test_achat_completion_forwards_usage_sink_through_thread_pool():
    """achat_completion 走 asyncio.to_thread，kwargs 必须原样透传——否则异步路径
    （workflow / tool_specs）永远拿不到 usage。"""
    client = _make_client(_response("正文", usage=_usage(31, 7)))
    seen = []

    result = asyncio.run(client.achat_completion([{"role": "user", "content": "x"}],
                                                 usage_sink=seen.append))

    assert result == "正文"
    assert seen == [{"prompt_tokens": 31, "completion_tokens": 7}]


def test_stream_completion_does_not_send_stream_options_by_default(monkeypatch):
    """默认关闭：老网关/老 vLLM 见到 stream_options 直接 400，默认打开等于
    把所有流式调用打成故障。"""
    monkeypatch.delenv("LLM_STREAM_INCLUDE_USAGE", raising=False)
    client = _make_client(_chunks("正", "文", final_usage=_usage(9, 3)))
    seen = []

    text = client.stream_completion([{"role": "user", "content": "x"}], usage_sink=seen.append)

    assert text == "正文"
    assert "stream_options" not in client.client.chat.completions.calls[0], \
        "未开开关就不得携带 stream_options"
    assert seen == [{"prompt_tokens": None, "completion_tokens": None}], \
        "没要到真值也要回调，让调用方知道该走估算"


def test_stream_completion_sends_stream_options_and_reports_usage_when_enabled(monkeypatch):
    monkeypatch.setenv("LLM_STREAM_INCLUDE_USAGE", "true")
    client = _make_client(_chunks("正", "文", final_usage=_usage(120, 40)))
    seen = []

    text = client.stream_completion([{"role": "user", "content": "x"}], usage_sink=seen.append)

    assert text == "正文"
    assert client.client.chat.completions.calls[0]["stream_options"] == {"include_usage": True}
    # 收尾块 choices 为空，usage 必须先于 choices 判断取到，否则真值被 continue 吃掉
    assert seen == [{"prompt_tokens": 120, "completion_tokens": 40}]


def test_stream_completion_per_call_include_usage_overrides_env(monkeypatch):
    monkeypatch.delenv("LLM_STREAM_INCLUDE_USAGE", raising=False)
    client = _make_client(_chunks("正", final_usage=_usage(4, 2)))
    seen = []

    client.stream_completion([{"role": "user", "content": "x"}],
                             usage_sink=seen.append, include_usage=True)

    assert client.client.chat.completions.calls[0]["stream_options"] == {"include_usage": True}
    assert seen == [{"prompt_tokens": 4, "completion_tokens": 2}]


def test_stream_completion_include_usage_false_is_the_400_escape_hatch(monkeypatch):
    """开关开了但某个后端不认时，调用点传 include_usage=False 即可单点绕过。"""
    monkeypatch.setenv("LLM_STREAM_INCLUDE_USAGE", "true")
    client = _make_client(_chunks("正文"))

    text = client.stream_completion([{"role": "user", "content": "x"}],
                                    usage_sink=lambda _u: None, include_usage=False)

    assert text == "正文"
    assert "stream_options" not in client.client.chat.completions.calls[0]


def test_stream_completion_without_sink_never_sends_stream_options(monkeypatch):
    """没人在账上就不发这个参数——即使环境开关是开的。"""
    monkeypatch.setenv("LLM_STREAM_INCLUDE_USAGE", "true")
    client = _make_client(_chunks("<think>推理</think>", "正文"))

    assert client.stream_completion([{"role": "user", "content": "x"}]) == "正文"
    assert "stream_options" not in client.client.chat.completions.calls[0]


def test_stream_completion_enabled_but_backend_sends_no_usage_chunk(monkeypatch):
    """后端认了参数却不回 usage：回调 None 值，绝不把 None 当 0 记账。"""
    monkeypatch.setenv("LLM_STREAM_INCLUDE_USAGE", "true")
    client = _make_client(_chunks("正文"))
    seen = []

    client.stream_completion([{"role": "user", "content": "x"}], usage_sink=seen.append)

    assert seen == [{"prompt_tokens": None, "completion_tokens": None}]


def test_scripted_client_reports_none_usage_by_default():
    """默认没有真值（维持既有 estimated=True 口径），但 usage_sink 照样收到回调。"""
    client = ScriptedLLMClient(script={"extract": ["结果"]})
    seen = []

    assert client.chat_completion(_msgs("extract"), usage_sink=seen.append) == "结果"
    assert seen == [{"prompt_tokens": None, "completion_tokens": None}]
    raw = client.chat_completion_raw(_msgs("extract"))
    assert raw["usage"] == {"prompt_tokens": None, "completion_tokens": None}


def test_scripted_client_can_simulate_real_usage():
    """配了 usage 就当 provider 回了真值：调用方据此不必再估算。"""
    client = ScriptedLLMClient(script={"extract": ["结果"]},
                              usage={"prompt_tokens": 120, "completion_tokens": 30})
    seen = []

    assert client.chat_completion(_msgs("extract"), usage_sink=seen.append) == "结果"
    assert seen == [{"prompt_tokens": 120, "completion_tokens": 30}]
    assert client.chat_completion_raw(_msgs("extract"))["usage"] == {
        "prompt_tokens": 120, "completion_tokens": 30}


def test_scripted_client_stream_forwards_usage_sink():
    client = ScriptedLLMClient(script={"extract": ["流式结果"]},
                              usage={"prompt_tokens": 7, "completion_tokens": 2})
    seen = []

    assert client.stream_completion(_msgs("extract"), usage_sink=seen.append) == "流式结果"
    assert seen == [{"prompt_tokens": 7, "completion_tokens": 2}]