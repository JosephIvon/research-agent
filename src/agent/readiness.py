"""交付就绪评估（delivery readiness）：统一裁决「当前信息是否足够交付」。

把三个散落的闸门（verify 门禁 / quality 门禁 / 引用校验闭环）收敛成一个
可测试的裁决，并暴露两个现状盲区：

- **维度级完成度**：哪些研究维度 completed / missing / unconfirmed。
  质量评分只有整体分，看不出「定价缺」还是「某个非关键字段没补齐」。
- **核心维度缺口**：任务声明关心的维度里，哪些还没满足。
  引用校验只判「报告自洽」，判不了「研究完整」——报告没写的维度不产生违规。

裁决（verdict）语义：

- `blocked`  硬性条件不满足，不可交付（来源覆盖率 / 证据条数 / 幻觉引用）
- `continue` 引用未通过且判定层识别到可补抓的缺口 → 值得继续
- `ready`    引用通过且无核心维度缺口 → 正常交付
- `degraded` 其余：引用不过已尽力 / 核心维度缺（报告自洽 ≠ 研究完整）

设计约束（与 `plan_gap_refetch` 同一纪律）：纯函数，不联网、不调 LLM、
不改全局状态。verdict 由调用方拿到的观测值驱动，**不改任何现有闸门的判定**——
这里是收敛汇报：把散落的判定映射成四态 + 可读依据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from src.agent.citations import EvidenceStore
from src.tools.quality_scorer import QualityScorer

VERDICT_BLOCKED = "blocked"
VERDICT_CONTINUE = "continue"
VERDICT_READY = "ready"
VERDICT_DEGRADED = "degraded"

# 维度状态
DIM_COMPLETED = "completed"
DIM_MISSING = "missing"
DIM_UNCONFIRMED = "unconfirmed"

# 运行时无任务声明时的保守核心维度；评测 case 用 expected.dimensions 覆盖
DEFAULT_CORE_DIMENSIONS: Sequence[str] = ("pricing", "features")

# 与质量评分同源的关键词表（只取 key + keywords，避免依赖 QualityScorer 全对象）
_DIMENSION_RULES = [
    {"key": d["key"], "keywords": d["keywords"]}
    for d in QualityScorer.DIMENSIONS
]


def _dimension_of(key: str, value: str) -> Optional[str]:
    """证据 → 所属维度。

    优先 **key 精确匹配维度名**：`extract_key_info` 提示词建议用标准 key
    （pricing/features/models/...），这是最可靠的信号。关键词匹配作兜底
    （与质量评分同源）——但注意 `"price"` 并不是 `"pricing"` 的子串，
    只靠关键词会把标准 key 的定价证据漏判成无维度。
    """
    k = (key or "").lower().strip()
    v = (value or "").lower()
    for dim in _DIMENSION_RULES:
        if k == dim["key"].lower():
            return dim["key"]
    for dim in _DIMENSION_RULES:
        if any(kw.lower() in k or kw.lower() in v for kw in dim["keywords"]):
            return dim["key"]
    return None


def _violation_dimensions(violations, store: EvidenceStore) -> Set[str]:
    """违规指向的维度：ungrounded/uncited 的 cited_ids 对应证据所属维度。

    unknown_citation（幻觉 id）不参与——补页面补不出幻觉 id，重生成会删，
    且它没有可归属的证据。
    """
    dims: Set[str] = set()
    for v in violations or []:
        if not isinstance(v, dict):
            continue
        if v.get("type") not in ("ungrounded_claim", "uncited_claim"):
            continue
        for eid in v.get("cited_ids") or []:
            ev = store.get(str(eid))
            if ev is None:
                continue
            dim = _dimension_of(ev.key, ev.value)
            if dim:
                dims.add(dim)
    return dims


def assess_dimension_gaps(
    evidence_store: EvidenceStore,
    violations: Optional[Sequence[Dict[str, Any]]] = None,
    dimensions: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    """维度级状态：completed / missing / unconfirmed。

    - missing      ：该维度无任何证据
    - unconfirmed  ：有证据，但被 ungrounded/uncited 违规指向（报告没用对）
    - completed    ：有证据且无违规指向

    维度集合默认取质量评分的全部 7 维。纯函数，可单测、可进评测断言。
    """
    store = evidence_store or EvidenceStore()
    dim_names = list(dimensions) if dimensions else [d["key"] for d in _DIMENSION_RULES]
    violated = _violation_dimensions(violations, store)

    status: Dict[str, str] = {}
    for dim in dim_names:
        has_evidence = any(
            _dimension_of(ev.key, ev.value) == dim for ev in store.evidences
        )
        if not has_evidence:
            status[dim] = DIM_MISSING
        elif dim in violated:
            status[dim] = DIM_UNCONFIRMED
        else:
            status[dim] = DIM_COMPLETED
    return status


@dataclass(frozen=True)
class DeliveryReadiness:
    """交付就绪裁决。全标量或纯列表，便于进 SSE 与落库。"""

    verdict: str
    reasons: List[str] = field(default_factory=list)
    hard_gates: List[str] = field(default_factory=list)
    dimensions: Dict[str, str] = field(default_factory=dict)
    critical_gaps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "hard_gates": list(self.hard_gates),
            "dimensions": dict(self.dimensions),
            "critical_gaps": list(self.critical_gaps),
        }


def assess_delivery_readiness(
    *,
    evidence_store: EvidenceStore,
    citation_passed: bool,
    violations: Optional[Sequence[Dict[str, Any]]] = None,
    source_coverage: Optional[float] = None,
    evidence_count: Optional[int] = None,
    competitor_count: Optional[int] = None,
    verification_passed: Optional[bool] = None,
    quality_usable: Optional[bool] = None,
    refetch_eligible: bool = False,
    core_dimensions: Sequence[str] = DEFAULT_CORE_DIMENSIONS,
    min_source_coverage: float = 0.8,
    min_evidence_items: int = 3,
    min_competitors: int = 2,
) -> DeliveryReadiness:
    """交付就绪统一裁决（纯函数）。

    参数全部是调用方可观测的当前状态；阈值是默认值，调用方可覆盖。
    不改现有闸门——这里是**收敛汇报**：映射成四态 + 可读依据。

    `refetch_eligible`：判定层是否识别到当前仍有可补抓的缺口。
    接线方在「已尝试补抓」后传 False（补抓只做一轮，不因同一缺口再继续）。
    """
    hard: List[str] = []
    if source_coverage is not None and source_coverage < min_source_coverage:
        hard.append(f"来源覆盖率 {source_coverage:.0%} < {min_source_coverage:.0%}")
    if evidence_count is not None and evidence_count < min_evidence_items:
        hard.append(f"证据 {evidence_count} 条 < {min_evidence_items}")
    if verification_passed is False:
        hard.append("抽取数据不足或缺来源")
    if quality_usable is False:
        hard.append("数据质量不足")
    if any(isinstance(v, dict) and v.get("type") == "unknown_citation"
           for v in (violations or [])):
        hard.append("存在幻觉引用（引用不存在的证据 id）")

    dimensions = assess_dimension_gaps(evidence_store, violations)
    critical = [
        d for d in core_dimensions
        if dimensions.get(d) in (DIM_MISSING, DIM_UNCONFIRMED)
    ]

    if hard:
        return DeliveryReadiness(
            verdict=VERDICT_BLOCKED, hard_gates=hard,
            dimensions=dimensions, critical_gaps=critical,
            reasons=["硬性条件不满足，中止交付"] + hard,
        )

    reasons: List[str] = []
    if not citation_passed and refetch_eligible:
        verdict = VERDICT_CONTINUE
        reasons.append("引用校验未通过且判定层识别到可补抓的缺口，值得继续")
    elif not citation_passed:
        verdict = VERDICT_DEGRADED
        reasons.append("引用校验未通过且已尽力（重生成/补抓），按缺口标注交付")
    elif critical:
        verdict = VERDICT_DEGRADED
        reasons.append(
            "引用自洽但核心维度存在缺口（" + "、".join(sorted(critical)) +
            "）——报告自洽 ≠ 研究完整，标记未确认交付"
        )
    elif competitor_count is not None and competitor_count < min_competitors:
        verdict = VERDICT_DEGRADED
        reasons.append(
            f"竞品样本不足（{competitor_count} < {min_competitors}），"
            "比较结论可信度受限，按降级交付"
        )
    else:
        verdict = VERDICT_READY
        reasons.append("引用校验通过且无核心维度缺口，可以交付")

    return DeliveryReadiness(
        verdict=verdict, reasons=reasons, hard_gates=hard,
        dimensions=dimensions, critical_gaps=critical,
    )
