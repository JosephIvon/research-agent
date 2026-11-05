"""上下文装配：token 估算、观察结果压缩、逐页 map-reduce 抽取。

map-reduce 是对 `[:LLM_MAX_CONTENT_CHARS]` 整体截断的替代——30 个页面拼成
一坨再掐掉 95% 是当前 legacy 链路最大的信息损失点；逐页抽取后归并零新依赖。

P3 上下文折叠（2026-11-04）：长任务把较早的 tool 观察摘要成「指针 + 内容哈希」，
原文存进 `EvidenceIndex`（内容寻址，键 = 内容 sha256 前缀），模型可随时用
`read_evidence(key, offset, limit)` 分页回读原文。折叠前后同一 key 的哈希不变，
因此"摘要不丢可回读证据"是可断言的，而不是靠承诺。
"""
import hashlib
import json
import math
from typing import Any, Callable, Dict, List, Optional

# 粗粒度 token 估算，用于折叠阈值与预算守卫：
# - ASCII / 其它非 CJK 字符沿用 4 字符/token 的启发式；
# - CJK（中日韩）按 1 字符 ≈ 1 token 计。
#
# 依据：用 tiktoken 在仓库稳定中文长文（124,375 字符）实测，o200k_base 约为本估算的
# 1.10 倍，cl100k_base 约为 1.31 倍（本估算仍低 ~24%）。取 1 是为了贴近更新的主流
# 分词器、避免对中英混排过度折叠；对 cl100k 类模型仍是已知残余偏差
# （折叠与预算守卫会略晚触发）。实测区间与偏差方向见
# docs/design/2026-11-05-context-fold-ab.md §3.6。
_CHARS_PER_TOKEN = 4

# CJK 统一表意文字、假名、谚文、全角标点/字母等。区间取常见 Unicode Blocks。
_CJK_RANGES = (
    (0x2E80, 0x2EFF),   # CJK 部首补充
    (0x2F00, 0x2FDF),   # 康熙部首
    (0x3000, 0x303F),   # CJK 标点
    (0x3040, 0x30FF),   # 平假名 + 片假名
    (0x3100, 0x312F),   # 注音
    (0x31A0, 0x31BF),   # 注音扩展
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x4E00, 0x9FFF),   # CJK 基本区
    (0xA960, 0xA97F),   # 谚文字母扩展 A
    (0xAC00, 0xD7AF),   # 谚文音节
    (0xF900, 0xFAFF),   # CJK 兼容表意文字
    (0xFE10, 0xFE1F),   # 竖排标点
    (0xFF01, 0xFF60),   # 全角 ASCII / 半角片假名
    (0x20000, 0x2FA1F), # CJK 扩展 B–F / 兼容补充
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    if code < 0x2E80:
        return False
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
    return False


# 分页回读的默认块大小（字符）。块小到能多次分页、又大到不需要上百轮。
EVIDENCE_CHUNK_CHARS = 2000


def estimate_tokens(text: Optional[str]) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return cjk + math.ceil(other / _CHARS_PER_TOKEN)


def render_observation(result: Dict[str, Any], char_budget: int = 6000) -> str:
    """把工具统一结果压成可塞回 LLM 上下文的文本。

    超预算时保头保尾并标注省略量——定价信息常在页首，结论常在页尾，
    掐中间比掐尾保留的信息密度高。
    """
    try:
        payload_obj = {
            "ok": result.get("ok"),
            "data": result.get("data"),
            "error": result.get("error"),
        }
        # 契约 v2 的加法字段只在存在时透出：旧调用（无游标/错误码）的观察文本不变。
        if result.get("nextOffset") is not None:
            payload_obj["nextOffset"] = result["nextOffset"]
        if result.get("error_code"):
            payload_obj["error_code"] = result["error_code"]
            if result.get("retryable"):
                payload_obj["retryable"] = True
        payload = json.dumps(payload_obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(result.get("data"))[:char_budget]

    if len(payload) <= char_budget:
        return payload

    head = int(char_budget * 0.7)
    tail = char_budget - head
    omitted = len(payload) - head - tail
    return (
        payload[:head]
        + f"\n…[省略 {omitted} 字符，完整内容保留在 workspace，可用 extract_facts 深挖]…\n"
        + payload[-tail:]
    )


class EvidenceIndex:
    """内容寻址的观察原文仓库：key 由内容哈希决定，同一内容永远同一 key。

    存在 workspace 里（`workspace["_evidence_index"]`），生命周期与一次
    Runner 运行相同——不落盘、不跨任务复用，崩溃恢复时原文本来就已从
    checkpoint 重建，无需另存一份。
    """

    def __init__(self, chunk_chars: int = EVIDENCE_CHUNK_CHARS):
        self._records: Dict[str, Dict[str, Any]] = {}
        self._chunk_chars = max(1, int(chunk_chars))
        # 被 read_evidence 回读过的 key。用于终态报告“折了但没人读”的证据量。
        self._read_keys: set[str] = set()

    def put(self, content: str) -> Dict[str, Any]:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        key = "ev_" + digest[:16]
        self._records[key] = {"content": content, "digest": digest}
        return {"key": key, "digest": digest, "chars": len(content)}

    def get(self, key: str) -> Optional[str]:
        record = self._records.get(key)
        return record["content"] if record else None

    def digest(self, key: str) -> Optional[str]:
        record = self._records.get(key)
        return record["digest"] if record else None

    def unread_keys(self) -> List[str]:
        """被折叠进索引、但从未被 read_evidence 回读的 key（按写入顺序）。"""
        return [key for key in self._records if key not in self._read_keys]

    def read_keys(self) -> set[str]:
        return set(self._read_keys)

    def read_page(self, key: str) -> Dict[str, Any]:
        """返回可被注册表按 offset/limit 切片的 chunks 列表。

        切片由 `ToolRegistry._slice_result` 做（复用既有游标），handler 只负责
        把原文切成连续块并给出**全量**哈希——所以哈希与是否分页无关。
        """
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"未知证据 key: {key!r}")
        self._read_keys.add(key)
        content = record["content"]
        chunks: List[Dict[str, Any]] = []
        for index, start in enumerate(range(0, len(content), self._chunk_chars)):
            end = min(start + self._chunk_chars, len(content))
            chunks.append({
                "index": index,
                "start": start,
                "end": end,
                "text": content[start:end],
            })
        return {
            "key": key,
            "digest": record["digest"],
            "chars": len(content),
            "chunks": chunks,
        }


_FOLD_NOTE = "原始观察已折叠；调用 read_evidence(key, offset, limit) 可分页回读原文"
_FOLD_MIN_CHARS_DEFAULT = 200


def fold_history(
    messages: List[Dict[str, Any]],
    index: EvidenceIndex,
    keep_recent: int = 4,
    min_chars: int = _FOLD_MIN_CHARS_DEFAULT,
) -> Dict[str, Any]:
    """把较早的长工具观察替换成「evidence_key + 哈希」指针，并追加一条摘要。

    - 只折 `role == "tool"` 且尚未折叠过的消息；最近 `keep_recent` 条不动。
    - 原文进 `index`，折叠后的 content 保留 key/digest/chars/tool_call_id，
      模型能据此精确回读，不靠猜。
    - 同一内容折叠两次得到同一 key（内容寻址），天然幂等。
    """
    cutoff = max(1, len(messages) - max(0, int(keep_recent)))
    folded: List[Dict[str, Any]] = []
    for message in messages[:cutoff]:
        if message.get("role") != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str) or len(content) < min_chars:
            continue
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("folded") or ("chunks" in parsed and "digest" in parsed):
                continue  # 已折叠，或本身就是 read_evidence 的回读结果
        meta = index.put(content)
        message["content"] = json.dumps(
            {
                "folded": True,
                "evidence_key": meta["key"],
                "digest": meta["digest"],
                "chars": meta["chars"],
                "tool_call_id": message.get("tool_call_id"),
                "note": _FOLD_NOTE,
            },
            ensure_ascii=False,
        )
        folded.append(meta | {"tool_call_id": message.get("tool_call_id")})

    if not folded:
        return {"folded": 0, "keys": []}

    lines = [
        "[上下文折叠] 为控制上下文长度，较早的工具观察已摘要为证据指针。",
        "原文未丢失：需要细节时调用 read_evidence(key, offset, limit) 分页回读。",
    ]
    for item in folded:
        lines.append(
            f"- key={item['key']} digest={item['digest'][:16]}… "
            f"chars={item['chars']} tool_call_id={item.get('tool_call_id')}"
        )
    summary = "\n".join(lines)
    messages.append({"role": "user", "content": summary})
    return {
        "folded": len(folded),
        "keys": [item["key"] for item in folded],
        "summary": summary,
    }


async def map_reduce_extract(
    pages: List[Dict[str, Any]],
    extract_fn: Callable[[str, str], Any],
    max_pages: int = 5,
) -> List[Dict[str, Any]]:
    """逐页抽取 → 归并去重。

    pages: [{url, content}, ...]；extract_fn(url, text) 返回该页的事实列表。
    去重键 = (key, value 的字符串形式)：不同页面常对同一事实各有半句描述，
    完全相同的重复保留首见（source_url 取首见页面）。
    """
    merged: List[Dict[str, Any]] = []
    seen = set()

    for page in pages[:max_pages]:
        url = page.get("url") or ""
        text = page.get("content") or ""
        if not text:
            continue
        try:
            items = await _maybe_await(extract_fn(url, text)) or []
        except Exception:
            continue  # 单页抽取失败不拖垮整体——归并的意义就在于此
        for item in items:
            if not isinstance(item, dict):
                continue
            dedup = (str(item.get("key", "")), str(item.get("value", "")))
            if dedup in seen:
                continue
            seen.add(dedup)
            item.setdefault("source_url", url)
            item.setdefault("confidence", 0.8)
            merged.append(item)

    return merged


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value
