"""P3 上下文折叠验收：摘要后仍能按指针对回原文，且哈希不变。

核心断言不是“折叠函数返回了几条”，而是：
- 折叠前后的同一 evidence key 回读出的内容 sha256 完全一致；
- 分页回读拼起来等于原文（offset/nextOffset 游标真的能翻到底）；
- 重复折叠幂等，不会把指针再套一层指针。
"""
import asyncio
import hashlib
import json

import pytest

from src.agent import config, tool_specs
from src.agent.context import EvidenceIndex, estimate_tokens, fold_history
from src.agent.runner import ReActRunner
from src.agent.tool_specs import build_default_tools
from src.agent.tools import ToolRegistry


def _tool_observation(content: str, call_id: str = "c1") -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _messages(text: str) -> list:
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "fetch_site", "arguments": "{}"}}]},
        _tool_observation(text),
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c2", "type": "function",
                         "function": {"name": "fetch_site", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "近期短观察"},
    ]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_estimate_tokens_is_cjk_aware():
    # CJK 按 1 字符 ≈ 1 token；ASCII 仍按 4 字符/token。
    assert estimate_tokens("中文" * 500) == 1000
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("中" * 200 + "a" * 400) == 300
    assert estimate_tokens("") == 0
    # 全角标点也属 CJK 区间（不能被当成 1/4 token 低估）
    assert estimate_tokens("。") == 1


class _FakeSearchTool:
    def __init__(self, results):
        self.results = results

    def search(self, query, num_results=None, provider=None):
        return self.results


class _FoldAwareLLM:
    """按上下文里是否出现折叠指针来决定行为，而不是照脚本走。

    两种模式：
    - allow_read=True：见到未读过的折叠 key 就调 read_evidence 回读，读完再终答；
    - allow_read=False：见到折叠 key 直接终答（引用来源 URL）。
    """

    provider_name = "fake"
    default_model = "fold-aware"

    def __init__(self, answer: str, allow_read: bool):
        self.answer = answer
        self.allow_read = allow_read
        self.search_calls = 0
        self.read_keys: set = set()
        self.calls: list = []

    @staticmethod
    def _folded_keys(messages) -> list:
        keys = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            try:
                parsed = json.loads(message.get("content") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict) and parsed.get("folded") and parsed.get("evidence_key"):
                keys.append(parsed["evidence_key"])
        return keys

    @staticmethod
    def _raw(calls=None, content=None) -> dict:
        return {
            "content": content,
            "tool_calls": calls or [],
            "finish_reason": "tool_calls" if calls else "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    def _tool_call(self, name: str, arguments: dict, index: int) -> dict:
        return {
            "id": f"call_{index}",
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        }

    async def achat_completion_raw(self, messages, tools=None, tool_choice="auto",
                                   temperature=0.2, max_tokens=4096, timeout=None):
        self.calls.append(list(messages))
        if self.search_calls < 2:
            self.search_calls += 1
            return self._raw([self._tool_call(
                "web_search", {"query": f"q{self.search_calls}", "num_results": 5},
                len(self.calls),
            )])

        folded = self._folded_keys(messages)
        if self.allow_read:
            pending = [key for key in folded if key not in self.read_keys]
            if pending:
                self.read_keys.update(pending)
                calls = [
                    self._tool_call(
                        "read_evidence", {"key": key, "offset": 0, "limit": 1},
                        f"{len(self.calls)}_{i}",
                    )
                    for i, key in enumerate(pending)
                ]
                return self._raw(calls)
        return self._raw(content=self.answer)


_FOLD_HITS = [
    {
        "title": f"竞品 {i}",
        "url": "https://acme.example.com" if i == 0 else f"https://p{i}.example.com",
        "content": "定价与功能说明" + "。" * 200,
    }
    for i in range(5)
]


def _run_fold_case(allow_read: bool):
    llm = _FoldAwareLLM(
        answer="## 结论\n\nAcme 定价见 https://acme.example.com 。",
        allow_read=allow_read,
    )
    workspace = {}
    tools = build_default_tools(
        llm,
        workspace,
        search_factory=lambda: _FakeSearchTool(list(_FOLD_HITS)),
        url_validator=lambda url: url,
    )
    runner = ReActRunner(llm, tools=tools, workspace=workspace, max_steps=10)
    runner.fold_tokens = 50
    runner.fold_keep_recent = 2
    runner.fold_min_chars = 200
    return runner, asyncio.run(runner.run("调研 acme 定价"))


def test_unread_folded_evidence_counts_when_never_read():
    runner, result = _run_fold_case(allow_read=False)

    assert result.stop_reason == "halt"
    # 折了但一次都没回读：计数必须非 0，且终答引用了来源 URL
    assert result.unread_folded_evidence >= 1
    assert result.unread_cited_evidence >= 1
    assert result.outcome.unread_folded_evidence == result.unread_folded_evidence
    assert result.outcome.unread_cited_evidence == result.unread_cited_evidence
    assert result.outcome.to_dict()["unread_folded_evidence"] >= 1
    # 折叠确实发生过（否则断言会因为“没折”而假通过）
    assert runner.evidence_index.unread_keys()


def test_unread_evidence_is_zero_when_all_folded_pages_read_back():
    runner, result = _run_fold_case(allow_read=True)

    assert result.stop_reason == "halt", result.stop_reason
    assert runner.evidence_index.read_keys(), "回读路径必须真的执行过"
    assert result.unread_folded_evidence == 0
    assert result.unread_cited_evidence == 0
    assert result.outcome.unread_folded_evidence == 0
    assert result.outcome.to_dict()["unread_cited_evidence"] == 0


def test_fold_keeps_same_content_hash_and_pointer():
    original = json.dumps(
        {"ok": True, "data": {"text": "定价 " + "x" * 3000}}, ensure_ascii=False
    )
    index = EvidenceIndex()
    messages = _messages(original)

    result = fold_history(messages, index, keep_recent=2, min_chars=200)

    assert result["folded"] == 1
    key = result["keys"][0]
    pointer = json.loads(messages[3]["content"])
    assert pointer["folded"] is True
    assert pointer["evidence_key"] == key
    # 折叠前（原文进索引时）与折叠后（按 key 回读）哈希必须一致
    assert index.digest(key) == _sha256(original)
    assert index.get(key) == original
    assert key in messages[-1]["content"], "摘要里必须留下可回读的指针"


def test_fold_is_idempotent():
    original = json.dumps({"ok": True, "data": {"text": "y" * 500}})
    index = EvidenceIndex()
    messages = _messages(original)

    first = fold_history(messages, index, keep_recent=2)
    second = fold_history(messages, index, keep_recent=2)

    assert first["folded"] == 1
    assert second["folded"] == 0
    assert index.get(first["keys"][0]) == original


def test_evidence_readback_paginates_to_same_digest():
    content = "A" * 5000
    workspace = {}
    registry = build_default_tools(None, workspace)
    index = workspace["_evidence_index"]
    key = index.put(content)["key"]

    chunks = []
    offset = 0
    pages = 0
    while offset is not None:
        result = asyncio.run(
            registry.execute("read_evidence", {"key": key, "offset": offset, "limit": 2})
        )
        assert result["ok"] is True, result
        chunks.extend(result["data"]["chunks"])
        offset = result["nextOffset"]
        pages += 1

    joined = "".join(chunk["text"] for chunk in chunks)
    assert pages >= 2, "5000 字符在 2000/块的默认块大小下必须翻页"
    assert joined == content
    assert _sha256(joined) == index.digest(key)


def test_runner_folds_over_threshold_and_keeps_pointer(monkeypatch):
    from src.agent import config

    monkeypatch.setattr(config, "CONTEXT_FOLD_TOKENS", 10)
    runner = ReActRunner(None, tools=ToolRegistry(), workspace={})
    runner.fold_keep_recent = 2
    runner.fold_min_chars = 200

    original = json.dumps({"ok": True, "data": {"text": "z" * 2000}})
    messages = _messages(original)
    runner._fold_if_needed(messages)

    # 最后一条是折叠摘要；原 tool 消息已变成指针
    pointer = json.loads(messages[3]["content"])
    key = pointer["evidence_key"]
    assert runner.evidence_index.digest(key) == _sha256(original)
    assert runner.evidence_index.get(key) == original
    assert len(messages[3]["content"]) < len(original), "折叠后的上下文必须真的变短"
