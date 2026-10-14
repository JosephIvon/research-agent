"""上下文装配：token 估算、观察结果压缩、逐页 map-reduce 抽取。

map-reduce 是对 `[:LLM_MAX_CONTENT_CHARS]` 整体截断的替代——30 个页面拼成
一坨再掐掉 95% 是当前 legacy 链路最大的信息损失点；逐页抽取后归并零新依赖。
"""
import json
import math
from typing import Any, Callable, Dict, List, Optional

# 粗粒度 token 估算：中英混合下 ~4 字符/token 是常用下界。
# 只用于预算守卫（宁严勿松），真实 usage（provider 返回）始终优先。
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: Optional[str]) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def render_observation(result: Dict[str, Any], char_budget: int = 6000) -> str:
    """把工具统一结果压成可塞回 LLM 上下文的文本。

    超预算时保头保尾并标注省略量——定价信息常在页首，结论常在页尾，
    掐中间比掐尾保留的信息密度高。
    """
    try:
        payload = json.dumps(
            {"ok": result.get("ok"), "data": result.get("data"), "error": result.get("error")},
            ensure_ascii=False,
            default=str,
        )
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
