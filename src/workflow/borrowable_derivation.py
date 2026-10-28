"""可借鉴点的**证据锚定推导**：域无关，不依赖任何领域规则表。

为什么不用规则表
----------------
按行业写规则表写不完，而且写错了方向：“可借鉴”难的不是"看懂对手在做什么"
（那是模式要点，已由抽取给出），而是"这件事**对我们**是否适用"。这条判断
依赖的是**我方上下文**（我们是谁、要解决什么、有什么约束），不是竞品属于
哪个行业。所以规则表这种"行业专用"的知识载体注定写不完。

这里的做法：让模型做**语义迁移**，让代码做**机械校验**。

    模式要点(有证据) ──模型──▶ {锚点key, 逐字片段, 借鉴动作}
                              │
                              └─代码校验─┬ 锚点 key 必须存在
                                       ├ 逐字片段必须出现在锚点原文里
                                       ├ 动作里的数字必须在锚点原文中出现
                                       ├ 动作必须是"做什么"，长度受限
                                       └ 最多三条、去重
                              │
                    校验不通过 → 丢弃这一条（不修、不猜、不留占位）

校验全是确定性规则，**与领域无关**：任何行业的竞品都能用同一套校验。
模型只负责"从这条证据出发，对我们意味着什么"——这正是模型强、规则弱的地方。

"有针对性"来自我方上下文
-------------------------
没有我方上下文时，模型只能给通用话（"可借鉴其仓配能力"）。因此本模块接受
``self_context``（我方产品/目标/约束的简短描述）；有它时判断才可能针对。
没有它也能跑，但矩阵里的借鉴点会偏通用——这是上下文缺失，不是规则缺失。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.agent.citations import NUMBER_RE, extract_numbers, make_evidence_id

DERIVE_INTENT = "borrowable"
MAX_ITEMS_PER_PLATFORM = 3
MAX_TAKE_LENGTH = 80
MIN_TAKE_LENGTH = 4
# 推导输出很短，但推理模型会把同一份预算花在思考上——沿用客户端默认 4096
# 时思考就能吃光，正文一个字都不剩（与抽取/分解同一个坑，这里单独给足）。
DERIVE_MAX_TOKENS = 8192

_SYSTEM_PROMPT = (
    "<!--intent:" + DERIVE_INTENT + "-->\n"
    "你是竞品分析评审助手。任务：从竞品的“模式要点”证据里，挑出**对我们**有借鉴意义的点。\n\n"
    "硬性要求：\n"
    "1. 每条借鉴点必须锚定到一条证据：给出 anchor_key（证据的 key）与 anchor_span\n"
    "   （该证据原文里的**逐字片段**，不要改写、不要省略中间内容）。\n"
    "2. 借鉴点写“我们该做什么/借鉴什么”，**不要复述对手做了什么**。\n"
    "   反例：“自建仓配网络”；正例：“多仓分层：按服务半径切仓网密度，远距离城市走外协”。\n"
    "3. 借鉴点里不要出现锚点原文中没有的数字（数字无法佐证）。\n"
    "4. 没把握就少给，宁缺毋滥；不要为了凑数写通用话。\n"
    "5. 如果给了“我方产品/目标”上下文，必须针对它判断；没有上下文时只写能由锚点\n"
    "   直接支撑的借鉴点。\n\n"
    "输出严格 JSON，不要 markdown 代码块：\n"
    '{"rows":[{"platform":"竞品名","items":[{"anchor_key":"证据key",'
    '"anchor_span":"逐字片段","take":"借鉴点"}]}]}\n'
)


@dataclass
class DerivedItem:
    """一条通过校验的借鉴点。"""

    take: str
    anchor_key: str
    anchor_span: str
    evidence_id: str = ""
    source_url: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "take": self.take,
            "anchor_key": self.anchor_key,
            "anchor_span": self.anchor_span,
            "evidence_id": self.evidence_id,
            "source_url": self.source_url,
            "origin": "evidence-anchored",
        }


@dataclass
class DerivationResult:
    items_by_platform: Dict[str, List[DerivedItem]] = field(default_factory=dict)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.items_by_platform)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items_by_platform": {
                platform: [item.to_dict() for item in items]
                for platform, items in self.items_by_platform.items()
            },
            "rejected": self.rejected,
            "error": self.error,
        }


def _normalize(text: str) -> str:
    """归一化空白与全角标点：模型常把原文里的空格/换行改掉。"""
    normalized = re.sub(r"\s+", "", str(text or ""))
    return normalized.replace("，", ",").replace("。", ".")


def _numbers(text: str) -> set:
    values = set()
    for token in extract_numbers(text or ""):
        try:
            values.add(Decimal(token.replace(",", "")))
        except (InvalidOperation, AttributeError):
            continue
    return values


def _usable_items(competitor: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = []
    for item in competitor.get("extracted_data") or []:
        if not isinstance(item, dict) or item.get("extraction_failed"):
            continue
        value = str(item.get("value", "") or "").strip()
        if not value or not str(item.get("source_url", "") or "").strip():
            continue
        items.append(item)
    return items


def _value_text(item: Dict[str, Any]) -> str:
    value = item.get("value")
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def build_messages(
    query: str,
    self_context: str,
    competitors: Sequence[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """构造推导请求：我方上下文 + 每个竞品的证据条目（key/value）。"""
    payload = []
    for comp in competitors:
        if comp.get("status") != "success":
            continue
        entries = []
        for item in _usable_items(comp):
            entries.append({
                "key": str(item.get("key", "")),
                "value": _value_text(item)[:600],
            })
        if entries:
            payload.append({
                "platform": str(comp.get("name") or comp.get("url") or ""),
                "url": str(comp.get("url") or ""),
                "entries": entries,
            })

    user = {
        "调研主题": query,
        "我方产品/目标/约束": (self_context or "").strip() or "（未提供：只写能由证据直接支撑的通用借鉴点）",
        "竞品证据": payload,
    }
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _parse_payload(response: Any) -> List[Dict[str, Any]]:
    """解析模型返回；容忍 ```json 围栏。"""
    text = response if isinstance(response, str) else str(response or "")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("推导响应不是 JSON")
        data = json.loads(text[start:end + 1])
    if isinstance(data, dict):
        rows = data.get("rows")
        data = rows if isinstance(rows, list) else []
    if not isinstance(data, list):
        raise ValueError("推导响应 rows 不是数组")
    return [row for row in data if isinstance(row, dict)]


def validate(
    rows: Sequence[Dict[str, Any]],
    competitors: Sequence[Dict[str, Any]],
) -> DerivationResult:
    """机械校验：把模型输出里站不住脚的条目丢掉。"""
    result = DerivationResult()
    by_platform = {}
    for comp in competitors:
        if comp.get("status") != "success":
            continue
        name = str(comp.get("name") or comp.get("url") or "")
        by_platform[name] = comp
        by_platform[str(comp.get("url") or "")] = comp

    seen_takes: Dict[str, set] = {}
    for row in rows:
        platform = str(row.get("platform") or "").strip()
        comp = by_platform.get(platform)
        if comp is None:
            # 平台名对不上：可能模型写了简称。允许用 url 命中一次
            comp = next((c for c in competitors if platform and platform in str(c.get("url", ""))), None)
        if comp is None:
            result.rejected.append({"platform": platform, "reason": "平台未在证据集合中", "take": ""})
            continue
        canonical = str(comp.get("name") or comp.get("url") or "")
        bucket = result.items_by_platform.setdefault(canonical, [])
        taken = seen_takes.setdefault(canonical, set())

        for item in row.get("items") or []:
            if not isinstance(item, dict):
                continue
            take = str(item.get("take") or "").strip()
            anchor_key = str(item.get("anchor_key") or "").strip()
            anchor_span = str(item.get("anchor_span") or "").strip()

            def reject(reason: str) -> None:
                result.rejected.append({
                    "platform": canonical, "reason": reason, "take": take, "anchor_key": anchor_key,
                })

            if not (MIN_TAKE_LENGTH <= len(take) <= MAX_TAKE_LENGTH):
                reject(f"take 长度 {len(take)} 不在 {MIN_TAKE_LENGTH}-{MAX_TAKE_LENGTH}")
                continue
            if take in taken:
                reject("take 重复")
                continue
            anchor = next(
                (i for i in _usable_items(comp)
                 if str(i.get("key", "")).strip().lower() == anchor_key.lower()),
                None,
            )
            if anchor is None:
                reject(f"锚点 key 不存在: {anchor_key}")
                continue
            anchor_text = _value_text(anchor)
            if not anchor_span or _normalize(anchor_span) not in _normalize(anchor_text):
                reject("锚点片段不在证据原文中")
                continue
            extra = _numbers(take) - _numbers(anchor_text)
            if extra:
                reject(f"动作含原文没有的数字: {', '.join(str(x) for x in sorted(extra))}")
                continue
            evidence_id = make_evidence_id(
                str(anchor.get("source_url", "")),
                str(anchor.get("key", "")),
                str(anchor.get("value", "") if not isinstance(anchor.get("value"), (list, tuple))
                    else " ".join(str(v) for v in anchor.get("value"))).strip(),
            )
            bucket.append(DerivedItem(
                take=take,
                anchor_key=anchor_key,
                anchor_span=anchor_span,
                evidence_id=evidence_id,
                source_url=str(anchor.get("source_url", "")),
            ))
            taken.add(take)
            if len(bucket) >= MAX_ITEMS_PER_PLATFORM:
                break

    # 每平台条数超限的，截断
    for platform, items in result.items_by_platform.items():
        result.items_by_platform[platform] = items[:MAX_ITEMS_PER_PLATFORM]
    # 全部被拒的平台直接删掉，避免出现空行
    result.items_by_platform = {k: v for k, v in result.items_by_platform.items() if v}
    return result


async def derive(
    llm_client: Any,
    query: str,
    self_context: str,
    competitors: Sequence[Dict[str, Any]],
) -> DerivationResult:
    """调用模型推导并校验；任何异常都返回空结果（调用方走兜底）。"""
    payload = [c for c in competitors if c.get("status") == "success" and _usable_items(c)]
    if not payload:
        return DerivationResult(error="没有可用证据")
    messages = build_messages(query, self_context, payload)
    try:
        response = await llm_client.achat_completion(
            messages, temperature=0.2, max_tokens=DERIVE_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 —— 推导失败不能拖垮整条链路
        return DerivationResult(error=f"LLM 调用失败: {type(exc).__name__}: {exc}")
    try:
        rows = _parse_payload(response)
    except Exception as exc:  # noqa: BLE001
        return DerivationResult(error=f"推导响应解析失败: {exc}")
    result = validate(rows, payload)
    if not result.ok:
        result.error = result.error or "推导结果全部未通过校验"
    return result
