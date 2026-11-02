"""结构化竞品矩阵及固定 Markdown 章节。

矩阵由确定性代码构造，不让模型自由补表。``columns_complete`` 只表示 schema
字段存在；``content_complete`` 才表示内容可用于交付：占位、无来源、无日期、
非法证据 id、未标为推断的可借鉴点或证据外数字均不算完整。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from src.agent.citations import NUMBER_RE, extract_numbers, make_evidence_id
from src.workflow.borrowable_rules import derive_borrowable

SCHEMA_VERSION = "competitor-matrix/v1"
EVIDENCE_GRADES = ("official", "media", "measured", "inferred", "unverified")
GRADE_LABELS = {
    "official": "官方",
    "media": "媒体",
    "measured": "实测",
    "inferred": "推断",
    "unverified": "未核实",
}
_GRADE_RANK = {grade: index for index, grade in enumerate(EVIDENCE_GRADES)}
REQUIRED_COLUMNS = ("platform", "positioning", "model_points", "borrowable")
UNVERIFIED = "未核实"
NON_ANSWER_RE = re.compile(
    r"^\s*(?:未公开|未披露|暂无公开|暂无|未知|待确认|待核实|无法确认|无法核实|"
    r"n/?a|none|null|—|-)\s*$",
    re.IGNORECASE,
)
_EVIDENCE_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_BORROWABLE_KEYS = {"borrowable", "可借鉴", "借鉴", "启示", "recommendation"}
# 一行最多几条可借鉴点：再多这一列就变成长清单，报告首屏读不动。
MAX_BORROWABLE_PER_ROW = 4


class MatrixValidationError(ValueError):
    """矩阵不满足结构 schema；与内容完整度判定分开。"""


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _is_non_answer(value: Any) -> bool:
    return isinstance(value, str) and bool(NON_ANSWER_RE.match(value))


def _has_effective_text(value: Any) -> bool:
    """占位/未核实标记不是有效内容，即使字符串非空。"""
    if not isinstance(value, str) or not value.strip() or _is_non_answer(value):
        return False
    normalized = value.strip().lower()
    return not (
        "待产品判断" in normalized
        or "[未核实]" in normalized
        or "[unverified]" in normalized
        or normalized.startswith("未核实")
    )


def _effective_values(value: Any) -> bool:
    values = value if isinstance(value, (list, tuple)) else [value]
    return bool(values) and all(_has_effective_text(item) for item in values)


def _is_inference(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith(("推断：", "推断:", "[推断]", "inferred:", "[inferred]"))


def _valid_evidence(evidence: Any) -> bool:
    if isinstance(evidence, EvidenceRef):
        evidence_id, url, grade = evidence.evidence_id, evidence.url, evidence.grade
    elif isinstance(evidence, dict):
        evidence_id = evidence.get("evidence_id")
        url, grade = evidence.get("url"), evidence.get("grade")
    else:
        return False
    return bool(
        _EVIDENCE_ID_RE.fullmatch(str(evidence_id or ""))
        and str(url or "").strip()
        and grade in EVIDENCE_GRADES
        and grade != "unverified"
    )


@dataclass
class EvidenceRef:
    evidence_id: str
    url: str
    key: str = ""
    quote: str = ""
    grade: str = "unverified"
    as_of: str = ""

    @property
    def citation(self) -> str:
        return f"[e:{self.evidence_id}]"

    @property
    def is_valid(self) -> bool:
        return _valid_evidence(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "url": self.url,
            "key": self.key,
            "quote": self.quote,
            "grade": self.grade,
            "as_of": self.as_of,
        }


@dataclass
class CompetitorRow:
    platform: str
    positioning: str
    model_points: List[str] = field(default_factory=list)
    borrowable: List[str] = field(default_factory=list)
    evidence: List[EvidenceRef] = field(default_factory=list)
    as_of: str = UNVERIFIED
    evidence_grade: str = "unverified"
    warnings: List[str] = field(default_factory=list)

    @property
    def columns_complete(self) -> bool:
        """结构完整：字段非空；占位行在这里仍可有值。"""
        return all(_has_value(getattr(self, name)) for name in REQUIRED_COLUMNS)

    @property
    def content_missing(self) -> List[str]:
        missing = [
            name for name in ("platform", "positioning", "model_points")
            if not _effective_values(getattr(self, name))
        ]
        if not _effective_values(self.borrowable) or not all(
            _is_inference(item) for item in self.borrowable
        ):
            missing.append("borrowable")
        if not any(ev.is_valid for ev in self.evidence):
            missing.append("evidence")
        if not _has_effective_text(self.as_of):
            missing.append("as_of")
        if self.evidence_grade == "unverified":
            missing.append("evidence_grade")
        return missing

    @property
    def content_complete(self) -> bool:
        """有效完整度：缺证据/日期、推断未标、数字未核实均不能通过。"""
        return not self.content_missing

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "positioning": self.positioning,
            "model_points": list(self.model_points),
            "borrowable": list(self.borrowable),
            "evidence": [ev.to_dict() for ev in self.evidence],
            "as_of": self.as_of,
            "evidence_grade": self.evidence_grade,
            "evidence_grade_label": GRADE_LABELS.get(self.evidence_grade, UNVERIFIED),
            "warnings": list(self.warnings),
            "columns_complete": self.columns_complete,
            "content_complete": self.content_complete,
            "content_missing": self.content_missing,
        }


@dataclass
class CompetitorMatrix:
    query: str
    rows: List[CompetitorRow] = field(default_factory=list)
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    schema_version: str = SCHEMA_VERSION
    # 研究计划摘要（主体/口径/重点角度）——报告必须让人看出
    # "系统认的是哪个主体、按什么口径选的竞品、重点看什么"，
    # 否则读者无从判断这张表该不该信。
    subject: Dict[str, Any] = field(default_factory=dict)

    @property
    def completeness(self) -> Dict[str, Any]:
        total = len(self.rows)
        structural = sum(1 for row in self.rows if row.columns_complete)
        complete = sum(1 for row in self.rows if row.content_complete)
        evidence_rows = sum(1 for row in self.rows if any(ev.is_valid for ev in row.evidence))
        as_of_rows = sum(1 for row in self.rows if _has_effective_text(row.as_of))
        return {
            "rows": total,
            "structural_complete_rows": structural,
            "complete_rows": complete,
            "completeness": round(complete / total, 3) if total else 0.0,
            "evidence_rows": evidence_rows,
            "as_of_rows": as_of_rows,
            "has_min_rows": total >= 2,
        }

    @property
    def is_complete(self) -> bool:
        summary = self.completeness
        return summary["has_min_rows"] and summary["completeness"] == 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "query": self.query,
            "generated_at": self.generated_at,
            "rows": [row.to_dict() for row in self.rows],
            "completeness": self.completeness,
            "subject": dict(self.subject),
        }


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[\n；;]+", value) if part.strip()]
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            out.extend(_as_list(item))
        return out
    return [str(value)]


def _clean_list(value: Any) -> List[str]:
    return [item for item in _as_list(value) if _has_effective_text(item)]


def _compact(text: Any, limit: int = 240) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _usable_item(item: Any) -> bool:
    return bool(
        isinstance(item, dict)
        and not item.get("extraction_failed")
        and str(item.get("source_url", "") or "").strip()
    )


def _pick(items: Sequence[Dict[str, Any]], keys: Sequence[str]) -> Optional[str]:
    lowered = {str(key).lower() for key in keys}
    for item in items:
        if not _usable_item(item) or str(item.get("key", "")).lower() not in lowered:
            continue
        value = item.get("value")
        if _has_effective_text(str(value or "")):
            return _compact(value)
    return None


def _pick_list(items: Sequence[Dict[str, Any]], keys: Sequence[str]) -> List[str]:
    lowered = {str(key).lower() for key in keys}
    for item in items:
        if not _usable_item(item) or str(item.get("key", "")).lower() not in lowered:
            continue
        values = _clean_list(item.get("value"))
        if values:
            return values
    return []


def _pick_items(
    items: Sequence[Dict[str, Any]], keys: Sequence[str]
) -> List[Tuple[str, Dict[str, Any]]]:
    """同 ``_pick_list``，但保留来源条目——推导可借鉴点要知道依据是哪条证据。"""
    lowered = {str(key).lower() for key in keys}
    picked: List[Tuple[str, Dict[str, Any]]] = []
    for item in items:
        if not _usable_item(item) or str(item.get("key", "")).lower() not in lowered:
            continue
        for value in _clean_list(item.get("value")):
            picked.append((value, item))
    return picked


def _grade_of(item: Dict[str, Any], comp: Optional[Dict[str, Any]] = None) -> str:
    key = str(item.get("key", "")).strip().lower()
    if key in _BORROWABLE_KEYS:
        return "inferred"
    explicit = str(item.get("evidence_grade") or item.get("source_type") or "").lower()
    if explicit in _GRADE_RANK:
        return explicit
    source_host = urlparse(str(item.get("source_url") or "")).netloc.lower()
    competitor_host = urlparse(str((comp or {}).get("url") or "")).netloc.lower()
    if source_host and source_host == competitor_host:
        return "official"
    return "unverified"


def _as_of_of(item: Dict[str, Any]) -> str:
    for key in ("as_of", "date", "retrieved_at", "口径日期"):
        value = item.get(key)
        if value and not _is_non_answer(value):
            return _compact(value, 40)
    if str(item.get("key", "")).strip().lower() in {
        "as_of", "date", "retrieved_at", "口径日期"
    }:
        value = item.get("value")
        if value and not _is_non_answer(value):
            return _compact(value, 40)
    return ""


def _platform_from(items: Sequence[Dict[str, Any]], comp: Dict[str, Any]) -> str:
    name = _pick(items, ("name", "platform", "品牌", "产品名", "title"))
    if name:
        return name
    if comp.get("name"):
        return str(comp["name"])
    host = urlparse(str(comp.get("url") or "")).netloc
    return host.removeprefix("www.") or "未命名竞品"


def _build_evidence(
    items: Sequence[Dict[str, Any]], comp: Optional[Dict[str, Any]] = None
) -> List[EvidenceRef]:
    refs: List[EvidenceRef] = []
    seen = set()
    for item in items:
        if not _usable_item(item):
            continue
        value = str(item.get("value", "") or "").strip()
        if not value:
            continue
        url = str(item.get("source_url", "") or "").strip()
        key = str(item.get("key", "") or "").strip()
        evidence_id = make_evidence_id(url, key, value)
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        refs.append(EvidenceRef(
            evidence_id=evidence_id,
            url=url,
            key=key,
            quote=_compact(value, 200),
            grade=_grade_of(item, comp),
            as_of=_as_of_of(item),
        ))
    return refs


def _number_value(token: str) -> Optional[Decimal]:
    try:
        return Decimal(token.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def _mark_unverified_numbers(
    text: str, evidence_values: Sequence[str]
) -> Tuple[str, List[str]]:
    """数字按数值 token 精确对账；``9`` 不得借证据里的 ``99`` 蒙混。"""
    evidence_numbers = {
        number
        for value in evidence_values
        for token in extract_numbers(value)
        for number in [_number_value(token)]
        if number is not None
    }
    missing = []
    for token in NUMBER_RE.findall(text or ""):
        number = _number_value(token)
        if number is not None and number not in evidence_numbers:
            missing.append(token)
    if not missing:
        return text, []
    return f"[未核实] {text}", sorted(set(missing))


def _as_inference(value: str) -> str:
    if _is_inference(value):
        return value
    if value.startswith("[未核实]"):
        suffix = value[len("[未核实]"):].strip()
        return f"[未核实] 推断：{suffix}"
    return f"推断：{value}"


def _row_from(
    comp: Dict[str, Any],
    items: Sequence[Dict[str, Any]],
    derived_items: Optional[Sequence[Dict[str, Any]]] = None,
) -> CompetitorRow:
    warnings: List[str] = []
    platform = _platform_from(items, comp)
    positioning = _pick(
        items, ("positioning", "定位", "description", "target_users", "目标用户")
    ) or UNVERIFIED
    if positioning == UNVERIFIED:
        warnings.append("缺定位：未找到带来源的 positioning/定位 证据")

    model_points = _pick_list(
        items,
        ("model_points", "business_model", "模式要点", "features", "功能", "核心功能"),
    ) or [UNVERIFIED]
    if model_points == [UNVERIFIED]:
        warnings.append("缺模式要点：未找到带来源的 model_points/features 证据")

    borrowable = _pick_list(items, tuple(_BORROWABLE_KEYS))
    derived: List[Dict[str, Any]] = []
    # 锚定借鉴点：模型给出「锚点 key + 逐字片段 + 借鉴动作」，代码已校验过
    # （锚点存在、片段逐字命中、数字有据）。这是**域无关**的路径——
    # 不需要为每个行业写一张规则表。
    anchored: List[Dict[str, Any]] = list(derived_items or [])
    if anchored:
        borrowable = [f"推断：{item['take']}" for item in anchored[:MAX_BORROWABLE_PER_ROW]]
        warnings.append(
            "可借鉴点由模型从证据锚定推导并经代码校验（推断，非竞品官方表述）"
        )
    elif not borrowable:
        # 抽取链路不会产出“可借鉴”——那是我们的产品判断，不是竞品事实。
        # 没有可用模型推导时，退回可审计的规则表从**有证据的模式要点**推导，
        # 命中不到就留空，绝不硬凑（硬凑出来的“可借鉴”正是早期报告里最像编造的段落）。
        model_pairs = _pick_items(
            items,
            ("model_points", "business_model", "模式要点", "features", "功能", "核心功能"),
        )
        derived = derive_borrowable(model_pairs)
        if derived:
            borrowable = [
                f"推断：{item['action']}" for item in derived[:MAX_BORROWABLE_PER_ROW]
            ]
            warnings.append(
                "可借鉴点由规则表从模式要点推导（推断，非竞品官方表述）："
                + "、".join(item["rule_id"] for item in derived)
            )
        else:
            borrowable = ["待产品判断（无直接证据）"]
            warnings.append("缺可借鉴点：未找到 borrowable/借鉴 证据，推导未产出，规则表也未命中，未编造")

    evidence = _build_evidence(items, comp)
    # 用完整证据原文对账，不能用渲染时截断到 200 字的 quote。
    evidence_values = [
        str(item.get("value", "") or "")
        for item in items
        if _usable_item(item)
    ]

    def _mark(values: List[str]) -> List[str]:
        out = []
        for value in values:
            marked, missing = _mark_unverified_numbers(value, evidence_values)
            if missing:
                warnings.append(f"数字未在任何证据中找到，已标未核实：{'、'.join(missing)}")
            out.append(marked)
        return out

    positioning = _mark([positioning])[0]
    model_points = _mark(model_points)
    borrowable = [_as_inference(value) for value in _mark(borrowable)]
    if anchored:
        # 证据引用放在数字校对之后追加：证据 id 含十六进制数字，
        # 先加进正文会被“证据外数字”判定误伤成未核实。
        borrowable = [
            f"{value}（据 [e:{item['evidence_id']}]）" if item.get("evidence_id") else value
            for value, item in zip(borrowable, anchored)
        ]
    elif derived:
        borrowable = [
            f"{value}（据 [e:{item['evidence_id']}]）" if item.get("evidence_id") else value
            for value, item in zip(borrowable, derived)
        ]

    as_of = next((date for item in items if (date := _as_of_of(item))), "")
    if not as_of:
        as_of = UNVERIFIED
        warnings.append("缺口径日期：未找到 as_of/date/retrieved_at 证据")
    if not evidence:
        warnings.append("该行没有带来源且可解析的证据引用，全部标为未核实")

    grades = [ref.grade for ref in evidence]
    grade = max(grades, key=lambda value: _GRADE_RANK[value]) if grades else "unverified"
    _detect_conflicts(items, warnings)
    return CompetitorRow(
        platform=platform,
        positioning=positioning,
        model_points=model_points,
        borrowable=borrowable,
        evidence=evidence,
        as_of=as_of,
        evidence_grade=grade,
        warnings=_dedupe(warnings),
    )


def _detect_conflicts(items: Sequence[Dict[str, Any]], warnings: List[str]) -> None:
    buckets: Dict[str, Dict[str, set]] = {}
    for item in items:
        if not _usable_item(item):
            continue
        key = str(item.get("key", "")).strip().lower()
        if key not in {"positioning", "pricing", "as_of", "name", "定价"}:
            continue
        value = _compact(item.get("value"), 120)
        if not _has_effective_text(value):
            continue
        sources = buckets.setdefault(key, {}).setdefault(value, set())
        sources.add(str(item.get("source_url", "")))
    for key, variants in buckets.items():
        if len(variants) < 2:
            continue
        source_count = len({url for sources in variants.values() for url in sources})
        label = "多来源冲突" if source_count >= 2 else "同源口径冲突"
        values = list(variants)
        warnings.append(f"{label}（{key}）：{' | '.join(values[:3])}")


def _dedupe(values: Iterable[str]) -> List[str]:
    out, seen = [], set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def build_matrix(
    query: str,
    extracted_data: Optional[Sequence[Dict[str, Any]]] = None,
    competitors: Optional[Sequence[Dict[str, Any]]] = None,
    derived_by_platform: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    subject: Optional[Dict[str, Any]] = None,
    officiality_by_host: Optional[Dict[str, str]] = None,
) -> CompetitorMatrix:
    """从抽取结果构造矩阵；competitors 保留“一家竞品一行”的边界。

    ``derived_by_platform`` 是**证据锚定**的借鉴点（模型推导 + 代码校验），
    键为竞品名；命中则优先于规则表兜底。
    ``officiality_by_host``：域名 → 官方性判定（App Store sellerUrl 交叉验证 ✓）。
    suspect（域非开发者站）行加 ⚠️ 警示——爬到的"定价/功能"可能是蹭名站的
    数据 ✗，读表的人要知道这一行的数据来源可疑；verified/unknown 不加注
    （不刷屏，信息在名额账/审计里有档 ✓）。
    """
    officiality_by_host = officiality_by_host or {}
    derived_map = derived_by_platform or {}

    def _derived_for(comp: Dict[str, Any]) -> List[Dict[str, Any]]:
        for key in (
            str(comp.get("name") or "").strip(),
            str(comp.get("url") or "").strip(),
        ):
            if key and derived_map.get(key):
                return list(derived_map[key])
        return []

    groups: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    if competitors:
        for comp in competitors:
            items = [item for item in (comp.get("extracted_data") or []) if isinstance(item, dict)]
            groups.append((comp, items))
    else:
        by_url: Dict[str, List[Dict[str, Any]]] = {}
        for item in extracted_data or []:
            if isinstance(item, dict):
                url = str(item.get("source_url", "") or "").strip()
                by_url.setdefault(url, []).append(item)
        for url, items in by_url.items():
            groups.append(({"url": url}, items))

    rows = [_row_from(comp, items, _derived_for(comp)) for comp, items in groups]
    for (comp, _items), row in zip(groups, rows):
        # 行上直接带 officiality（mvp_workflow 注入 ✓）优先，映射兜底（测试/直调 ✓）
        verdict = (str(comp.get("officiality") or "")
                   or officiality_by_host.get(_host_key(str(comp.get("url") or ""))))
        if verdict == "suspect":
            row.warnings.append(
                "官方性存疑：该域不是开发者站（App Store sellerUrl 不一致）——"
                "本行数据可能来自蹭名站，采信前先核对域主 ⚠️")
    matrix = CompetitorMatrix(
        query=query,
        rows=rows,
        subject=dict(subject or {}),
    )
    validate(matrix)
    return matrix


def _host_key(url: str) -> str:
    """取主机名（小写、去 www. 前缀），与发现阶段的 officiality 映射对齐 ✓。"""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def validate(matrix: CompetitorMatrix) -> None:
    """只校验结构 schema；有效内容由 content_complete/Metrics 分开判定。"""
    if not isinstance(matrix, CompetitorMatrix):
        raise MatrixValidationError("matrix 必须是 CompetitorMatrix")
    for index, row in enumerate(matrix.rows):
        if not _has_value(row.platform):
            raise MatrixValidationError(f"第 {index + 1} 行缺 platform")
        for name in REQUIRED_COLUMNS:
            if not _has_value(getattr(row, name)):
                raise MatrixValidationError(f"{row.platform} 缺列 {name}")
        if not isinstance(row.model_points, list) or not isinstance(row.borrowable, list):
            raise MatrixValidationError(f"{row.platform} 的 model_points/borrowable 必须是数组")
        if row.evidence_grade not in EVIDENCE_GRADES:
            raise MatrixValidationError(f"{row.platform} 的证据等级非法: {row.evidence_grade}")
        for evidence in row.evidence:
            if not _EVIDENCE_ID_RE.fullmatch(evidence.evidence_id):
                raise MatrixValidationError(f"{row.platform} 的证据 id 非法: {evidence.evidence_id}")
            if evidence.grade not in EVIDENCE_GRADES:
                raise MatrixValidationError(f"{row.platform} 的证据等级非法: {evidence.grade}")


def markdown(matrix: CompetitorMatrix) -> str:
    """渲染固定七列；冲突和其他告警直接留在对应行的证据列。"""
    lines = [
        "| 平台 | 定位 | 模式要点 | 可借鉴点 | 证据 | 口径日期 | 证据等级 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    if not matrix.rows:
        lines.append("| 未核实 | 未核实 | 未核实 | 未核实 | 未核实 | 未核实 | 未核实 |")
        return "\n".join(lines)

    for row in matrix.rows:
        model = "<br>".join(_cell(value) for value in row.model_points) or UNVERIFIED
        borrow = "<br>".join(_cell(value) for value in row.borrowable) or UNVERIFIED
        refs = []
        for evidence in row.evidence[:3]:
            refs.append(f"[e:{evidence.evidence_id}]({evidence.url})")
        if len(row.evidence) > 3:
            refs.append(f"+{len(row.evidence) - 3}")
        refs.extend(f"⚠️ {_cell(warning)}" for warning in row.warnings)
        evidence = "<br>".join(refs) if refs else UNVERIFIED
        lines.append(
            f"| {_cell(row.platform)} | {_cell(row.positioning)} | {model} | {borrow} | "
            f"{evidence} | {_cell(row.as_of)} | "
            f"{GRADE_LABELS.get(row.evidence_grade, UNVERIFIED)} |"
        )
    return "\n".join(lines)


def _cell(value: Any) -> str:
    return str(value or UNVERIFIED).replace("|", "\\|").replace("\n", " ")


def render_report_section(matrix: CompetitorMatrix) -> str:
    return (
        "<!--machine-appendix-->\n"
        "## 竞品模式速览\n\n"
        "> 本节由 `competitor_matrix` 结构化结果渲染，不是模型自由撰写；"
        "可借鉴点是推断而非官方事实，缺证据/日期或证据外数字标“未核实”。\n\n"
        + _render_subject(matrix.subject)
        + f"{markdown(matrix)}\n"
        "<!--/machine-appendix-->\n"
    )


def _render_subject(subject: Dict[str, Any]) -> str:
    """研究计划摘要行：主体是谁、按什么口径、重点看什么。"""
    if not subject:
        return ""
    parts: List[str] = []
    name = str(subject.get("entity_name") or "").strip()
    what = str(subject.get("what_it_is") or "").strip()
    if name:
        parts.append(f"**研究主体**：{name}" + (f"（{what}）" if what else ""))
    entity_type = str(subject.get("entity_type") or "").strip()
    if entity_type and entity_type != "unknown":
        parts.append(f"实体类型：{entity_type}")
    confidence = subject.get("resolved_confidence")
    if isinstance(confidence, (int, float)):
        parts.append(f"消歧置信度：{confidence:.2f}")
    candidates = [str(c) for c in (subject.get("disambiguation_candidates") or []) if str(c).strip()]
    if candidates:
        parts.append("备选：" + "、".join(candidates[:3]))
    layers = subject.get("layers") or []
    if layers:
        parts.append("竞品口径：" + "、".join(str(l) for l in layers))
    if subject.get("min_count") or subject.get("max_count"):
        parts.append(f"数量口径：{subject.get('min_count')}-{subject.get('max_count')} 家")
    # 实际执行的准入条件：报告里写的那句口径，代码判的是哪几条、哪条判不了。
    # 不写这一行，读者只能看到一句没人执行的自然语言。
    rule = subject.get("admission_rule") or {}
    executable = [str(c) for c in (rule.get("executable") or []) if str(c).strip()]
    if executable:
        parts.append("实际执行的入选条件：" + "；".join(executable[:4]))
    not_executable = [str(c) for c in (rule.get("not_executable") or []) if str(c).strip()]
    if not_executable:
        parts.append("未能执行的条件：" + "；".join(not_executable[:2]))
    rejected = subject.get("rejected_competitors") or []
    if rejected:
        names = "、".join(
            str(r.get("name") or r.get("url") or "") for r in rejected[:5]
        )
        parts.append(f"未采信（数据不足已剔除）：{names}")
    angles = subject.get("angles") or []
    if angles:
        parts.append("重点角度：" + "、".join(
            f"{a.get('label') or a.get('angle')} {round(float(a.get('weight') or 0) * 100)}%"
            for a in angles[:4]
        ))
    if not parts:
        return ""
    return "> " + "｜".join(parts) + "\n\n"


_SECTION_RE = re.compile(
    r"^(?:<!--machine-appendix-->\s*\n)?##\s*竞品模式速览\s*\n.*?"
    r"(?:<!--/machine-appendix-->\s*\n)?(?=^##\s|\Z)",
    re.DOTALL | re.MULTILINE,
)


def inject_section(
    report: str,
    matrix: CompetitorMatrix,
    position: str = "start",
) -> str:
    """幂等替换同名章节（含机器附录边界），再插入结构化固定章节。

    ``position``：
    - ``start``（legacy 默认）：速览表在前——读者先看结论表再看叙述
    - ``end``（ReAct 用）：速览表在后——ReAct 的报告正文是模型终答，
      合同是"报告以模型交付物开头"（低可信前缀都不能加在它前面），
      机器生成的快照跟在后面，与机器附录标记的语义一致
    """
    body = _SECTION_RE.sub("", report or "")
    section = render_report_section(matrix)
    if position == "end":
        return body.rstrip() + "\n\n" + section
    return section + "\n" + body.lstrip()
