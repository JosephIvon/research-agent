"""缺口定向补抓的判定：纯函数，无 I/O、无 LLM、无全局状态。

## 它回答什么

**哪个竞品还有没解决的数值缺口，按缺口密度排序，值得再花几页抓取。**

## 它明确不回答什么

**不回答「该去抓哪个页面」。** 这是刻意的边界，不是遗漏：

页面选择需要「哪些页面更可能含有某类信息」的业务先验，而任何这类先验在
开放领域都无法穷举。早期版本试图用「维度词表」解决它——把证据 key 映射到
标准维度，再由维度导出站内关键词。那张表在封闭集合（竞品定价/功能/模型）
上能撑住，一出这个集合就漏：抽取器的标准 key 只是提示词建议
（`mvp_workflow.py` 的 JSON schema 里 `key` 没有 enum），模型写出
`pricing_tier_v2`、`rate_limits`、`sla` 之类一律落进 unknown。

更糟的是它引入了**语义推断**：由「数字在哪」推出「这条主张有没有据」。
同站另一个维度里的 99 可能是限流而不是定价，用它判「证据已在手」是错的。

所以这里的边界是：**判定只用结构信号**——违规类型、站点归属、缺口数量、
额度。抓哪一页交给第 2 步，那一步必须自带可测的选页策略（现有
`discover_research_links` 的静态权重表**尚未经过检验**，见 §缺口）。

## 缺口数字只用于观测

`missing_numbers` 会被带进决策对象，但**判定过程从不读它**。
它的用途是事后测量：抓完之后回头看这些数字有没有出现在新证据里。
那是度量，不是决策 —— 且对开放领域免疫。

## 设计纪律

判定必须全确定性：不联网、不调 LLM、不读时钟。理由与 `citations.py` 开头一致——
让模型判「这个缺口值不值得补」就是循环论证，而且每次结果可能不同，
门禁就无法复现。

## 为什么单独一个模块，而不是并进 citations.py

`citations.py` 只做一件事：判定报告与证据是否自洽。这里是另一件事——
拿着校验结果决定「要不要再花钱抓」。依赖面、变更节奏、失败模式都不同
（一个失误是误判报告有罪，一个是误判该不该花钱）。拆开后各自可独立测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from src.agent.citations import EvidenceStore

__all__ = [
    "GapRefetchCandidate",
    "GapRefetchDecision",
    "plan_gap_refetch",
    "site_of",
]


# --------------------------------------------------------------------------
# 站点标识
# --------------------------------------------------------------------------

def site_of(url: str) -> str:
    """URL → 站点标识（小写 netloc）。

    归属判定必须按**站点**而不是整串 URL：同一竞品的定价页与功能页是两条证据、
    两个 URL，但缺口只可能属于这一个竞品。按 URL 比会把同一竞品拆成多个对象，
    抓取计划也会重复预算。
    """
    text = (url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    netloc = (parsed.netloc or "").lower()
    if netloc:
        return netloc
    # 相对 URL / 无 scheme：退化为去掉尾部斜杠的整串，仍是确定性的
    return text.rstrip("/").lower()


# --------------------------------------------------------------------------
# 原因码（全部是结构性的，没有一个依赖词表或语义推断）
# --------------------------------------------------------------------------

REASON_ELIGIBLE = "eligible"
REASON_PAGE_BUDGET_ZERO = "page_budget_zero"
REASON_NO_UNGROUNDED_CLAIM = "no_ungrounded_claim"
REASON_VIOLATION_NOT_STRUCTURED = "violation_not_structured"
REASON_UNRESOLVABLE_CITATION = "unresolvable_citation"
REASON_AMBIGUOUS_TARGET = "ambiguous_target"

# 并列时的固定优先级，保证同一输入永远得到同一个 reason（门禁可复现的前提）
_SKIP_PRIORITY: Tuple[str, ...] = (
    REASON_VIOLATION_NOT_STRUCTURED,
    REASON_UNRESOLVABLE_CITATION,
    REASON_AMBIGUOUS_TARGET,
)


@dataclass(frozen=True)
class GapRefetchCandidate:
    """一个值得再看看的竞品。字段全是标量或纯列表，便于进 SSE 与落库。"""

    competitor_url: str          # 站点标识（netloc），不是页面 URL
    competitor_name: str         # 展示名，认不出来时留空
    missing_numbers: List[str]   # 仅供观测/事后测量，判定过程不读
    claim_count: int             # 该站点上的 ungrounded 数量 → 排序依据
    per_candidate_pages: int = 1 # 该竞品分到几页额度


@dataclass(frozen=True)
class GapRefetchDecision:
    eligible: bool
    reason: str
    candidates: List[GapRefetchCandidate] = field(default_factory=list)
    page_budget: int = 0
    # 观测用：每种跳过原因的计数。空转率全靠它——只记 eligible/不 eligible
    # 两个取值，看不出「判不准」和「压根没候选」的区别。
    skipped: Dict[str, int] = field(default_factory=dict)
    seen_violation_types: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "page_budget": self.page_budget,
            "skipped": dict(self.skipped),
            "seen_violation_types": dict(self.seen_violation_types),
            "candidates": [
                {
                    "competitor_url": c.competitor_url,
                    "competitor_name": c.competitor_name,
                    "missing_numbers": list(c.missing_numbers),
                    "claim_count": c.claim_count,
                    "per_candidate_pages": c.per_candidate_pages,
                }
                for c in self.candidates
            ],
        }


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------

def _dominant_skip(skipped: Dict[str, int]) -> str:
    """跳过原因的主因：取计数最高者，并列按固定优先级，保证可复现。

    早先版本把主因写死成一个码，把不同原因全塌缩成同一个——而 reason 码存在的
    唯一意义就是回答「为什么不补抓」。塌缩之后线上根本分不清是归属不明、
    引用不可解析，还是结构化字段回归了。
    """
    if not skipped:
        return REASON_NO_UNGROUNDED_CLAIM
    return max(
        skipped.items(),
        key=lambda kv: (kv[1], -_SKIP_PRIORITY.index(kv[0])
                        if kv[0] in _SKIP_PRIORITY else 0),
    )[0]


def plan_gap_refetch(
    violations: Sequence[Dict[str, Any]],
    store: EvidenceStore,
    target_sites: Optional[Sequence[Dict[str, Any]]] = None,
    max_pages: int = 3,
) -> GapRefetchDecision:
    """判断引用校验未通过后，是否值得做一次定向补抓。

    第一版**只处理 `ungrounded_claim`**：`unknown_citation` 是引用 id 幻觉，
    `uncited_claim` 只是没标引用——这两类优先交给已有的「带违规清单重生成」处理。
    这是一个**收窄的产品策略**，不是对这两类成因的因果判断。

    参数
    ----
    violations : `verify_report` 返回的 `violations`
    store      : 与 violations 同源的 `EvidenceStore`
    target_sites: 可选。只取 `url` / `name` 做归属确认与展示名。
                 **登录凭据等字段一律不读、不进决策对象**（决策会被 SSE 发出去）。
    max_pages  : 本次补抓的总页数额度

    返回
    ----
    `GapRefetchDecision`。`eligible=False` 时 `reason` 说明为什么不做。
    纯函数：不联网、不调 LLM、不改全局状态。
    """
    seen: Dict[str, int] = {}
    for v in violations or []:
        vtype = str((v or {}).get("type", "")) or "unknown"
        seen[vtype] = seen.get(vtype, 0) + 1

    def _decision(reason: str, skipped: Optional[Dict[str, int]] = None,
                  page_budget: int = 0) -> GapRefetchDecision:
        return GapRefetchDecision(
            eligible=False, reason=reason, candidates=[], page_budget=page_budget,
            skipped=skipped or {}, seen_violation_types=seen,
        )

    if max_pages <= 0:
        return _decision(REASON_PAGE_BUDGET_ZERO, {}, page_budget=0)

    ungrounded = [
        v for v in (violations or [])
        if isinstance(v, dict) and v.get("type") == "ungrounded_claim"
    ]
    if not ungrounded:
        return _decision(REASON_NO_UNGROUNDED_CLAIM,
                         {k: n for k, n in seen.items() if k != "ungrounded_claim"},
                         page_budget=max_pages)

    # 站点白名单：只认 url / name，凭据字段不碰
    in_scope: Dict[str, str] = {}
    for site in (target_sites or []):
        if not isinstance(site, dict):
            continue
        key = site_of(str(site.get("url") or ""))
        if key:
            in_scope.setdefault(key, str(site.get("name") or ""))

    skipped: Dict[str, int] = {}

    def _skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    # 按站点聚合
    grouped: Dict[str, Dict[str, Any]] = {}

    for v in ungrounded:
        missing = v.get("missing_numbers")
        if not isinstance(missing, (list, tuple)) or not missing:
            _skip(REASON_VIOLATION_NOT_STRUCTURED)
            continue

        cited_ids = v.get("cited_ids")
        if not isinstance(cited_ids, (list, tuple)) or not cited_ids:
            _skip(REASON_VIOLATION_NOT_STRUCTURED)
            continue

        cited = []
        for eid in cited_ids:
            ev = store.get(str(eid))
            if ev is None:
                # 证据库与违规清单不同源：多半是调用方把两次校验的 store 搞混了。
                # 这种情况**不猜**。
                cited = None
                break
            cited.append(ev)
        if not cited:
            _skip(REASON_UNRESOLVABLE_CITATION)
            continue

        sites = {site_of(ev.url) for ev in cited}
        sites.discard("")
        if len(sites) != 1:
            # 0 = 证据没标来源；>1 = 一句引了多家，缺口归属不清
            _skip(REASON_AMBIGUOUS_TARGET)
            continue
        site = next(iter(sites))
        if in_scope and site not in in_scope:
            _skip(REASON_AMBIGUOUS_TARGET)
            continue

        bucket = grouped.setdefault(site, {"numbers": [], "claims": 0})
        bucket["claims"] += 1
        for token in missing:
            token = str(token)
            if token not in bucket["numbers"]:
                bucket["numbers"].append(token)

    if not grouped:
        return _decision(_dominant_skip(skipped), skipped, page_budget=max_pages)

    # 缺口多的竞品优先：纯结构信号（未解决数值主张的条数），不含任何语义判断
    ordered: List[str] = sorted(
        grouped, key=lambda s: (-grouped[s]["claims"], s)
    )
    budget = min(max_pages, len(ordered))
    per = max(1, budget // len(ordered))

    candidates = [
        GapRefetchCandidate(
            competitor_url=site,
            competitor_name=in_scope.get(site, ""),
            missing_numbers=grouped[site]["numbers"],
            claim_count=grouped[site]["claims"],
            per_candidate_pages=per,
        )
        for site in ordered
    ]

    return GapRefetchDecision(
        eligible=True, reason=REASON_ELIGIBLE, candidates=candidates,
        page_budget=budget, skipped=skipped, seen_violation_types=seen,
    )
