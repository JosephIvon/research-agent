"""RecordingLLMClient 录制-回放 round-trip 测试。

核心契约：录出的 JSON 与 ScriptedLLMClient 的脚本格式逐字兼容——
「真跑一次（内层脚本客户端模拟真模型）→ 录制落盘 → 脚本客户端加载
同一文件重放」的输出必须与原始调用一致。这是「L3 真跑轨迹沉淀回
L2 离线回放」这条路成立的前提，任何一侧的格式漂移都该在这里红。
"""
import json

import pytest

from src.llm import (
    RecordingLLMClient,
    ScriptedLLMClient,
    get_llm_client,
)

MSG = [
    {"role": "system", "content": "<!--intent:report-->写报告"},
    {"role": "user", "content": "竞品：Acme"},
]


def _marker(intent: str):
    return [
        {"role": "system", "content": f"<!--intent:{intent}-->"},
        {"role": "user", "content": "payload"},
    ]


class TestRoundTrip:
    def test_text_response_round_trips(self, tmp_path):
        """纯文本：录制条目是 str，回放返回同一文本。"""
        inner = ScriptedLLMClient(script={"report": ["定价 49 美元。"]})
        rec = RecordingLLMClient(inner, str(tmp_path / "rec.json"))

        out = rec.chat_completion(MSG)
        assert out == "定价 49 美元。"

        replay = ScriptedLLMClient(script=json.loads(
            (tmp_path / "rec.json").read_text(encoding="utf-8")
        ))
        assert replay.chat_completion(MSG) == "定价 49 美元。"

    def test_raw_single_tool_call_round_trips(self, tmp_path):
        """raw 单 tool_call：归一化为 tool_call 条目，回放 raw 输出等价。"""
        inner = ScriptedLLMClient(script={"react": [
            {"type": "tool_call", "name": "web_search",
             "arguments": '{"query": "acme pricing"}'},
        ]})
        rec = RecordingLLMClient(inner, str(tmp_path / "rec.json"))

        raw = rec.chat_completion_raw(_marker("react"))
        assert raw["tool_calls"][0]["name"] == "web_search"

        recorded = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
        assert recorded["react"] == [
            {"type": "tool_call", "name": "web_search",
             "arguments": '{"query": "acme pricing"}'},
        ]
        replay = ScriptedLLMClient(script=recorded)
        replayed = replay.chat_completion_raw(_marker("react"))
        assert replayed["tool_calls"][0]["name"] == "web_search"
        assert json.loads(replayed["tool_calls"][0]["arguments"]) == {"query": "acme pricing"}

    def test_raw_openai_function_shape_normalized(self, tmp_path):
        """OpenAI function 嵌套形态（真客户端 raw 的返回结构）也能归一化。"""
        class _RawStub(ScriptedLLMClient):
            def chat_completion_raw(self, messages, **kwargs):
                return {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "fetch_site",
                                                      "arguments": '{"url": "https://a.test"}'}},
                        {"id": "call_2", "function": {"name": "web_search",
                                                      "arguments": '{"query": "b"}'}},
                    ],
                    "finish_reason": "tool_calls",
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                }

        rec = RecordingLLMClient(_RawStub(script={"react": ["x"]}))
        rec.chat_completion_raw(_marker("react"))

        assert rec.script["react"] == [{"type": "tool_calls", "calls": [
            {"type": "tool_call", "name": "fetch_site",
             "arguments": '{"url": "https://a.test"}'},
            {"type": "tool_call", "name": "web_search", "arguments": '{"query": "b"}'},
        ]}]

    def test_extract_structured_round_trips(self, tmp_path):
        """extract 返回 dict：录制侧 json.dumps 成 str，回放侧还原为同一 dict。"""
        expected = [{"key": "pricing", "value": "$49"}]

        class _ExtractStub(ScriptedLLMClient):
            def extract_structured_data(self, content, schema):
                return expected

        rec = RecordingLLMClient(_ExtractStub(script={"report": ["x"]}), str(tmp_path / "rec.json"))
        out = rec.extract_structured_data("页面正文", {"type": "object"})
        assert out == expected

        recorded = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
        # dict 条目必须以 str 形态落盘：直接存 dict 会被脚本构造校验拒绝
        assert all(isinstance(e, str) for e in recorded["extract_structured"])

        replay = ScriptedLLMClient(script=recorded)
        assert replay.extract_structured_data("页面正文", {"type": "object"}) == expected

    def test_calls_preserve_order_within_intent(self, tmp_path):
        """同一意图多次调用按序追加——意图+序号是回放的对齐锚点。"""
        inner = ScriptedLLMClient(script={"report": ["第一版。", "第二版。"]})
        rec = RecordingLLMClient(inner, str(tmp_path / "rec.json"))

        rec.chat_completion(MSG)
        rec.chat_completion(MSG)

        recorded = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
        assert recorded["report"] == ["第一版。", "第二版。"]


class TestBestEffort:
    def test_unknown_intent_passes_through_without_recording(self, tmp_path):
        """识别不出意图：透传结果，不落盘、不抛异常。"""
        class _PermissiveStub(ScriptedLLMClient):
            """不校验意图的替身：任何消息都回固定文本（模拟真客户端行为）。"""
            def chat_completion(self, messages, **kwargs):
                return "文本"

        rec = RecordingLLMClient(_PermissiveStub(script={"report": ["x"]}), str(tmp_path / "rec.json"))

        out = rec.chat_completion([
            {"role": "system", "content": "没有任何意图标记的系统提示"},
            {"role": "user", "content": "x"},
        ])
        assert out == "文本"
        assert rec.script == {}
        assert not (tmp_path / "rec.json").exists()

    def test_dump_is_atomic_and_creates_directory(self, tmp_path):
        """dump 自动建目录；产物是合法 JSON（写一半被读到的窗口由 .tmp+rename 消除）。"""
        rec = RecordingLLMClient(ScriptedLLMClient(script={"report": ["x"]}))
        target = tmp_path / "nested" / "dir" / "rec.json"
        rec._record("文本", intent="report")
        rec.dump(str(target))
        assert json.loads(target.read_text(encoding="utf-8")) == {"report": ["文本"]}
        assert not target.with_suffix(".json.tmp").exists()


class TestWiring:
    def test_get_llm_client_wraps_when_env_set(self, tmp_path, monkeypatch):
        """LLM_RECORD_SCRIPT 设置时 get_llm_client 自动包装；不设时原样返回。"""
        path = str(tmp_path / "rec.json")
        monkeypatch.setenv("LLM_RECORD_SCRIPT", path)
        wrapped = get_llm_client("fake")
        assert isinstance(wrapped, RecordingLLMClient)

        monkeypatch.delenv("LLM_RECORD_SCRIPT")
        plain = get_llm_client("fake")
        assert not isinstance(plain, RecordingLLMClient)

    def test_attributes_proxy_to_inner(self):
        """default_model 等属性经 __getattr__ 代理到被包装客户端。"""
        rec = RecordingLLMClient(ScriptedLLMClient(script={"report": ["x"]}))
        assert rec.default_model == "scripted"
        assert rec.api_key == "not-needed"

    @pytest.mark.asyncio
    async def test_async_wrapper_records_too(self, tmp_path):
        """achat_completion 走 ABC 的线程池包装，仍经过被覆写的同步方法——录制不漏。"""
        inner = ScriptedLLMClient(script={"report": ["异步文本。"]})
        rec = RecordingLLMClient(inner, str(tmp_path / "rec.json"))

        out = await rec.achat_completion(MSG)
        assert out == "异步文本。"
        assert rec.script == {"report": ["异步文本。"]}
