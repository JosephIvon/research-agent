"""缺口定向补抓的判定：纯函数，无 I/O、无 LLM、无全局状态。

## 为什么单独一个模块，而不是并进 citations.py

`citations.py` 已经 890+ 行且只做一件事：判定报告与证据是否自洽。
这里是**另一件事**：拿着校验结果决定「要不要再花钱抓一轮」——依赖面不同、
变更节奏不同、失败模式也不同（一个是误判报告有罪，一个是误判该不该花钱）。
拆开之后两者能各自被独立测试，不需要为了测一个而把另一个的夹具一起搭起来。

## 设计纪律

判定必须**全确定性**：不联网、不调 LLM、不读时钟。理由与 `citations.py` 开头一致——
让模型判「这个缺口值不值得补」就是循环论证，而且每次判定结果可能不同，
门禁就无法复现。

确定性不等于语义正确。本模块只回答「补抓是否**值得一试**」，
它不证明两个数字语义相同，也不证明补抓一定能命中。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from src.agent.citations import Evidence, EvidenceStore

__all__ = [
    "GapRefetchCandidate",
    "GapRefetchDecision",
    "plan_gap_refetch",
    "site_of",
]


# --------------------------------------------------------------------------
# 维度词表（全确定性，无模型）
#
# 标准维度沿用抽取提示词里的 key 清单（`mvp_workflow.py` 的 extract_key_info
# 提示词：pricing / features / models / target_users / integrations /
# api_flow / workflow / limitations / reviews），并收窄常见同义写法。
#
# 页面类型词沿用爬虫的静态权重表（`web_crawler.py` 的 priority_terms）。
# 两套词本就部分重叠（pricing / models / api），这里取并集：
# **boost 只影响候选排序，不放宽同源与 SSRF 规则。**
# --------------------------------------------------------------------------

UNMAPPED_DIMENSION = "unknown"

_DIMENSION_ALIASES: Dict[str, str] = {
    # 标准 key 原样
    "pricing": "pricing",
    "features": "features",
    "models": "models",
    "target_users": "target_users",
    "integrations": "integrations",
    "api_flow": "api_flow",
    "workflow": "workflow",
    "limitations": "limitations",
    "reviews": "reviews",
    # 常见同义写法：模型并不总是照标准 key 填
    "price": "pricing",
    "cost": "pricing",
    "plan": "pricing",
    "plans": "pricing",
    "billing": "pricing",
    "feature": "features",
    "model": "models",
    "provider": "models",
    "providers": "models",
    "engines": "models",
    "integration": "integrations",
    "connector": "integrations",
    "connectors": "integrations",
    "user": "target_users",
    "users": "target_users",
    "customer": "target_users",
    "customers": "target_users",
    "audience": "target_users",
    "api": "api_flow",
    "sdk": "api_flow",
    "quota": "api_flow",
    "rate_limit": "api_flow",
    "ratelimit": "api_flow",
    "limits": "api_flow",
    "workflows": "workflow",
    "flow": "workflow",
    "routing": "workflow",
    "limitation": "limitations",
    "faq": "limitations",
    "review": "reviews",
    "testimonials": "reviews",
    # 兜底：抽取器自己塞的内容摘要
    "content": UNMAPPED_DIMENSION,
    "content_summary": UNMAPPED_DIMENSION,
}

# 维度 → 站内页面类型关键词。喂给 discover_research_links 做有界加分。
_DIMENSION_TERMS: Dict[str, Tuple[str, ...]] = {
    "pricing": ("pricing", "price", "plans", "billing", "cost", "定价", "价格", "计费", "套餐"),
    "api_flow": ("api", "sdk", "rate limit", "quota", "limits", "integration",
                 "接口", "限流", "配额", "集成"),
    "models": ("models", "providers", "engines", "模型", "供应商"),
    "integrations": ("integrations", "connectors", "sdk", "integration", "集成", "连接器"),
    "workflow": ("workflow", "flow", "route", "routing", "工作流", "流程", "路由"),
    "features": ("features", "guide", "docs", "功能", "文档", "指南"),
    "target_users": ("customers", "use cases", "solutions", "用户", "场景", "客户"),
    "limitations": ("faq", "limits", "help", "support", "限制", "常见问题", "帮助", "支持"),
    "reviews": ("reviews", "testimonials", "评价", "口碑"),
}

# 维度认不出来时的通用兜底词。只用来兜底，不代表「什么页面都值得抓」——
# 真正的收敛靠 page_budget 与同源限制。
_GENERIC_TERMS: Tuple[str, ...] = ("docs", "guide", "pricing", "features")


def dimension_of(key: str) -> str:
    """证据 key → 标准维度。认不出来统一归 `unknown`，不猜。"""
    return _DIMENSION_ALIASES.get((key or "").strip().lower(), UNMAPPED_DIMENSION)


def dimensions_compatible(a: str, b: str) -> bool:
    """两个维度是否可能描述同一件事。

    有一方认不出来时**判兼容**：宁可多补抓一次也不能因为词表缺词就把真缺口
    判成 `ambiguous_match`——那会让整个功能静默失效，而且失效原因极难察觉。
    收紧方向由 page_budget 和注入防护兜着，不靠这一条。
    """
    return a == b or UNMAPPED_DIMENSION in (a, b)


def boost_terms_for(dimension: str) -> List[str]:
    """维度 → 站内候选页关键词。认不出来的维度给通用词。"""
    return list(_DIMENSION_TERMS.get(dimension, _GENERIC_TERMS))


def site_of(url: str) -> str:
    """URL → 站点标识（小写 netloc）。

    归属判定必须按**站点**而不是整串 URL：同一竞品的定价页与功能页是两条证据、
    两个 URL，但缺口只可能属于这一个竞品。按 URL 比会把同一竞品拆成两个对象，
    进而把「A 竞品的缺口」错记到「A 的 /pricing 页」名下。
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
# 判定结果
# --------------------------------------------------------------------------

# 原因码（确定性、可枚举、可统计）
REASON_ELIGIBLE = "eligible"
REASON_ALREADY_AVAILABLE = "already_available"
REASON_AMBIGUOUS_TARGET = "ambiguous_target"
REASON_AMBIGUOUS_MATCH = "ambiguous_match"
REASON_VIOLATION_NOT_STRUCTURED = "violation_not_structured"
REASON_NO_ELIGIBLE_VIOLATION = "no_eligible_violation"
REASON_PAGE_BUDGET_ZERO = "page_budget_zero"


@dataclass(frozen=True)
class GapRefetchCandidate:
    """一个可补抓的缺口。字段全是标量或纯列表，便于直接进 SSE 与落库。"""

    competitor_url: str          # 该缺口归属的站点（site 标识，不是页面 URL）
    competitor_name: str         # 展示名，认不出来时留空
    dimension: str               # 标准维度，认不出来是 "unknown"
    missing_numbers: List[str]
    boost_terms: List[str]
    per_candidate_pages: int = 1 # 该缺口分到几页额度


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
                    "dimension": c.dimension,
                    "missing_numbers": list(c.missing_numbers),
                    "boost_terms": list(c.boost_terms),
                    "per_candidate_pages": c.per_candidate_pages,
                }
                for c in self.candidates
            ],
        }


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------

def _resolve_cited(violation: Dict[str, Any],
                   store: EvidenceStore) -> Optional[List[Evidence]]:
    """解析违规里 cited_ids 指向的证据。任一 id 查不到就返回 None。

    查不到说明证据库与违规清单不同源——多半是调用方把两次校验的 store 搞混了。
    这种情况**不猜**，直接判不可归属。
    """
    cited_ids = violation.get("cited_ids")
    if not isinstance(cited_ids, (list, tuple)) or not cited_ids:
        return None
    resolved: List[Evidence] = []
    for eid in cited_ids:
        ev = store.get(str(eid))
        if ev is None:
            return None
        resolved.append(ev)
    return resolved


def _dimension_of_cited(cited: Sequence[Evidence]) -> str:
    """所引证据的公共维度。

    取所有所引证据维度的**交集**：一句同时引 `pricing` 和 `models` 的证据时，
    它到底在主张哪个维度是不确定的，交集为空即 `unknown`，交给补抓去试。
    """
    dims = {dimension_of(ev.key) for ev in cited}
    dims.discard(UNMAPPED_DIMENSION)
    if not dims:
        return UNMAPPED_DIMENSION
    if len(dims) == 1:
        return next(iter(dims))
    return UNMAPPED_DIMENSION


def _numbers_available_elsewhere(
    store: EvidenceStore,
    site: str,
    dimension: str,
    missing: Sequence[str],
    exclude_ids: Set[str],
) -> Tuple[bool, Optional[str]]:
    """目标站点上是否**已有**可用证据包含这些数值。

    返回 `(是否已有, 命中的证据维度或 None)`。

    为什么要卡维度：只按数字匹配会把「别处的 99」当成「这里有 99」。
    那个 99 可能是 API 限额、用户数、版本号，甚至是另一家竞品的价钱——
    误判成 `already_available` 就等于把真缺口当成已解决，永远不会触发补抓。
    """
    try:
        targets = [float(t) for t in missing]
    except (TypeError, ValueError):
        # 数值 token 一定可转 float（同 citations._safe_float 的前提）；
        # 转不动说明上游传了脏数据，宁可判已有也不去猜。
        return True, dimension
    if not targets:
        return False, None

    matched_dim: Optional[str] = None
    for ev in store.evidences:
        if ev.evidence_id in exclude_ids:
            continue
        if site_of(ev.url) != site:
            continue
        ev_dim = dimension_of(ev.key)
        if not dimensions_compatible(ev_dim, dimension):
            continue
        if all(t in ev.numbers for t in targets):
            matched_dim = ev_dim
            break
    return (matched_dim is not None, matched_dim)


# 跳过原因的主因判定：取计数最高者；并列时按固定优先级，
# 保证同一输入永远得到同一个 reason（门禁可复现的前提）。
_SKIP_PRIORITY: Tuple[str, ...] = (
    REASON_VIOLATION_NOT_STRUCTURED,
    REASON_AMBIGUOUS_TARGET,
    REASON_AMBIGUOUS_MATCH,
    REASON_ALREADY_AVAILABLE,
)


def _dominant_skip(skipped: Dict[str, int]) -> str:
    if not skipped:
        return REASON_NO_ELIGIBLE_VIOLATION
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
                 **登录凭据等字段一律不读、不进决策对象**（SSE 会把决策发出去）。
    max_pages  : 本次补抓的总页数额度

    返回
    ----
    `GapRefetchDecision`。`eligible=False` 时 `reason` 说明为什么不做。
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
        skipped = {k: n for k, n in seen.items() if k != "ungrounded_claim"}
        skipped.setdefault(REASON_NO_ELIGIBLE_VIOLATION, 0)
        return _decision(REASON_NO_ELIGIBLE_VIOLATION, skipped, page_budget=max_pages)

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

    # 候选按 (站点, 维度) 聚合：同一竞品同一维度的多个缺口共用一次抓取
    grouped: Dict[Tuple[str, str], List[str]] = {}
    order: List[Tuple[str, str]] = []

    for v in ungrounded:
        missing = v.get("missing_numbers")
        if not isinstance(missing, (list, tuple)) or not missing:
            _skip(REASON_VIOLATION_NOT_STRUCTURED)
            continue

        cited = _resolve_cited(v, store)
        if not cited:
            _skip(REASON_AMBIGUOUS_TARGET)
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

        dimension = _dimension_of_cited(cited)
        exclude = {ev.evidence_id for ev in cited}
        available, matched_dim = _numbers_available_elsewhere(
            store, site, dimension, [str(m) for m in missing], exclude
        )
        if available:
            # 证据已在手但引错了——第三次报告生成换引用就能修，不用花钱抓
            _skip(REASON_ALREADY_AVAILABLE if matched_dim is not None
                  else REASON_AMBIGUOUS_MATCH)
            continue

        bucket = (site, dimension)
        if bucket not in grouped:
            grouped[bucket] = []
            order.append(bucket)
        for token in missing:
            token = str(token)
            if token not in grouped[bucket]:
                grouped[bucket].append(token)

    if not grouped:
        # 最终 reason 必须如实反映**主因**。早先这里写成"不是 already_available
        # 就一律 ambiguous_match"，把 ambiguous_target / violation_not_structured
        # 全塌缩成同一个码——而 reason 码存在的唯一意义就是回答"为什么不补抓"。
        # 塌缩之后线上看到 ambiguous_match 分不清是维度歧义、归属不明还是字段缺失。
        return _decision(_dominant_skip(skipped), skipped, page_budget=max_pages)

    budget = min(max_pages, len(order))
    # 额度不够分时**均分并至少各 1 页**：多于候选数就浪费，少于就饿死某个缺口
    per = max(1, budget // len(order))

    candidates: List[GapRefetchCandidate] = []
    for bucket in order:
        site, dimension = bucket
        candidates.append(GapRefetchCandidate(
            competitor_url=site,
            competitor_name=in_scope.get(site, ""),
            dimension=dimension,
            missing_numbers=grouped[bucket],
            boost_terms=boost_terms_for(dimension),
            per_candidate_pages=per,
        ))

    return GapRefetchDecision(
        eligible=True, reason=REASON_ELIGIBLE, candidates=candidates,
        page_budget=budget, skipped=skipped, seen_violation_types=seen,
    )
