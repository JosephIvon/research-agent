"""维度 → 候选页面的映射（dimension refetch）。

把「报告没写的维度」（missing / unconfirmed，不产生 ungrounded 违规、
判定层看不到）转成**该抓哪一页**的行动计划。与 `plan_gap_refetch` 边界清晰：

- `plan_gap_refetch`：处理数值缺口（报告**写了**但无据），站点归属靠 cited_ids。
- `plan_dimension_refetch`：处理维度缺口（报告**没写**），站点归属靠 per-site 维度状态。

两级映射（不硬编码 URL，站点 URL 结构千差万别）：

1. **意图层**：`DIMENSION_PAGE_TIERS`——维度 → `priority_tiers`，与
   `discover_research_links` 的 `DEFAULT_PRIORITY_TIERS` 同构，直接注入爬虫的
   站内发现排序（先验不是真理：换行业应整表覆盖，观测校准）。
2. **匹配层**：爬虫 discover 时按维度意图排序取 top 页——补抓一页通常就能
   闭合一个维度缺口（/pricing 一页搞定定价）。

与 `plan_gap_refetch` 同一纪律：纯函数，不联网、不调 LLM、不改全局状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from src.agent.citations import EvidenceStore
from src.agent.gap_refetch import REASON_ELIGIBLE
from src.agent.readiness import (
    DIM_MISSING,
    DIM_UNCONFIRMED,
    DEFAULT_CORE_DIMENSIONS,
    assess_dimension_gaps,
)

REASON_NO_CORE_DIMENSION_GAP = "no_core_dimension_gap"
REASON_NO_SITES = "no_sites"
REASON_BUDGET_INSUFFICIENT = "budget_insufficient"

# 维度 → 站内页面意图（priority_tiers 格式，权重风格对齐 discover 先验）。
# pricing/features/models 精调，其余单意图兜底。缺了哪个维度都能补抓。
DIMENSION_PAGE_TIERS: Dict[str, Tuple[Tuple[int, Tuple[str, ...]], ...]] = {
    "pricing": (
        (10, ("定价", "价格", "套餐", "pricing", "price", "plans", "subscription")),
        (5, ("收费", "月费", "年费", "billing")),
    ),
    "features": (
        (10, ("功能", "feature", "产品", "product", "capabilities", "特性")),
        (5, ("一览", "overview", "solution")),
    ),
    "models": (
        (10, ("模型", "model", "llm", "gpt", "claude")),
        (5, ("额度", "配额", "limits", "quota", "tokens")),
    ),
    "target_users": (
        (10, ("客户", "案例", "customers", "solutions", "use cases", "who it's for")),
        (5, ("行业", "适用", "industry", "audiences")),
    ),
    "integrations": (
        (10, ("集成", "integrations", "连接器", "connectors", "生态", "市场")),
        (5, ("api", "webhook", "插件", "plugin")),
    ),
    "limitations": (
        (10, ("限制", "限额", "limits", "rate", "quota", "threshold", "约束")),
        (5, ("文档", "docs", "documentation", "faq")),
    ),
    "reviews": (
        (10, ("评价", "评测", "reviews", "testimonials", "对比", "口碑")),
        (5, ("博客", "blog", "社区", "community", "案例研究")),
    ),
}


def _host(url: str) -> str:
    return urlparse(url or "").netloc.lower()


def assess_site_dimension_gaps(
    evidence_store: EvidenceStore,
    sites: Sequence[Dict[str, Any]],
    dimensions: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, str]]:
    """每站维度状态（completed/missing/unconfirmed），按证据 URL host 分组。

    `readiness` 的维度状态是全局的，但补抓是 per-site 的——"站点 A 有定价、
    站点 B 没有"必须落到"补抓 B"。host 匹配与 `plan_gap_refetch` 的
    competitor_url 归属口径一致。

    **刻意不接收 violations**：per-site 状态只可能是 completed/missing，
    永远不会是 unconfirmed。这是职责划分，不是疏漏——

    - `unconfirmed`（有证据但被 ungrounded/uncited 违规指向）的根因是
      **报告引用错/没引**，不是**证据缺**——现有证据已在该维度；
    - 修复路径是**重生成**（M2 带违规清单修引用）与**数值补抓**
      （ungrounded → `plan_gap_refetch`）；
    - 维度补抓的价值只在**证据缺**（missing）——补抓意图页带回新证据。
    把 violations 传进来让 unconfirmed 参与补抓，等于对"引用错"重复补抓，
    且 ambiguous（一句引多家）时站点归属不清，不猜。
    """
    store = evidence_store or EvidenceStore()
    dim_names = list(dimensions) if dimensions else list(DIMENSION_PAGE_TIERS)

    out: Dict[str, Dict[str, str]] = {}
    for site in sites or []:
        host = _host(site.get("url") or "")
        if not host:
            continue
        # key 是评测/结构化场景的标识；API 直达的 target_sites 可能没有 key，
        # 回退到 host（与判定层的 competitor_url 归属口径一致）。
        key = str(site.get("key") or "") or host
        site_evs = [
            ev for ev in store.evidences if _host(ev.url) == host
        ]
        per_store = EvidenceStore()
        for ev in site_evs:
            per_store.add(url=ev.url, key=ev.key, value=ev.value)
        out[key] = assess_dimension_gaps(per_store, dimensions=dim_names)
    return out


@dataclass(frozen=True)
class DimensionRefetchCandidate:
    """单个站点需要补抓的维度缺口与页面意图。"""

    site_key: str
    competitor_url: str
    dims: Tuple[str, ...]
    page_tiers: Tuple[Tuple[int, Tuple[str, ...]], ...]
    pages: int


@dataclass(frozen=True)
class DimensionRefetchDecision:
    """维度补抓决策。与 `GapRefetchDecision` 同风格：全标量，便于 SSE/落库。"""

    eligible: bool
    reason: str
    candidates: Tuple[DimensionRefetchCandidate, ...] = ()
    skipped: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "candidates": [
                {
                    "site_key": c.site_key,
                    "competitor_url": c.competitor_url,
                    "dims": list(c.dims),
                    "pages": c.pages,
                }
                for c in self.candidates
            ],
            "skipped": list(self.skipped),
        }


def _merge_tiers(dims: Sequence[str]) -> Tuple[Tuple[int, Tuple[str, ...]], ...]:
    """把多个缺失维度的意图 tiers 合并成一个（一页可覆盖多个维度缺口）。

    同一权重档位去重合并；不同档位按权重降序拼接，让高权重意图优先命中。
    """
    merged: Dict[int, set] = {}
    for dim in dims:
        for weight, terms in DIMENSION_PAGE_TIERS.get(dim, ()):
            merged.setdefault(weight, set()).update(terms)
    return tuple(
        (weight, tuple(sorted(terms))) for weight, terms in sorted(merged.items(), reverse=True)
    )


def plan_dimension_refetch(
    evidence_store: EvidenceStore,
    sites: Sequence[Dict[str, Any]],
    core_dimensions: Sequence[str] = DEFAULT_CORE_DIMENSIONS,
    max_pages: int = 3,
    per_site_pages: int = 2,
) -> DimensionRefetchDecision:
    """维度缺口 → 行动计划（纯函数）。

    - 每站算维度状态，核心维度里 missing/unconfirmed 的 → 候选。
    - 候选页意图 = 缺失维度的 tiers 合并；per-site 抓取页数默认 2
      = **首页 + 1 个维度意图命中的站内页**。
      ⚠️ 不能是 1：`fetch_site_profile(max_pages=N)` 抓首页 + discover
      `max(0, N-1)` 个站内页——N=1 时站内页 0 个，page_tiers 选页意图
      完全失效，维度补抓退化成"只重抽首页"（真实 bug，测试被夹具替身
      覆写 fetch_site_profile 掩盖）。
    - 预算：候选站数 ≤ `max_pages`（成本刹车，与 ungrounded 补抓同一量级）。
    """
    if not sites:
        return DimensionRefetchDecision(eligible=False, reason=REASON_NO_SITES)

    site_status = assess_site_dimension_gaps(evidence_store, sites, core_dimensions)
    candidates: List[DimensionRefetchCandidate] = []
    for site in sites:
        host = _host(site.get("url") or "")
        key = str(site.get("key") or "") or host
        status = site_status.get(key, {})
        missing = tuple(
            d for d in core_dimensions
            if status.get(d) in (DIM_MISSING, DIM_UNCONFIRMED)
        )
        if not missing:
            continue
        candidates.append(DimensionRefetchCandidate(
            site_key=key,
            competitor_url=str(site.get("url") or ""),
            dims=missing,
            page_tiers=_merge_tiers(missing),
            pages=per_site_pages,
        ))
        if len(candidates) >= max_pages:
            break

    if not candidates:
        return DimensionRefetchDecision(
            eligible=False, reason=REASON_NO_CORE_DIMENSION_GAP,
        )

    skipped = tuple(
        d for d in core_dimensions if not any(d in c.dims for c in candidates)
    )
    return DimensionRefetchDecision(
        eligible=True,
        reason=REASON_ELIGIBLE,
        candidates=tuple(candidates),
        skipped=skipped,
    )
