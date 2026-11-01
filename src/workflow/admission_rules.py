"""准入规则：把计划里那句自然语言口径，变成代码**真的在执行**的规则。

为什么要有这个模块（2026-10-28 真实搜索实测 + 文档 `docs/admission-metric-gap.md`）：

计划里的入选标准是模型写的自然语言，代码执行的是另一套规则——**声明与执行脱节**。
报告里写着「必须有官网 + 至少 2 个维度有数据」，代码当时判的却是"有独立域名"
（媒体站也有独立域名），于是知乎/应用商店/收录目录都能当竞品。

这里做的不是"再写一张规则表"，而是把规则**按可观测阶段拆开**：

| 条款形态 | 执行阶段 | 判据 |
|---|---|---|
| 必须有官网 | 发现阶段 | ``site_type == product_site`` |
| 至少 N 个维度有数据 | **抓后阶段** | 该家抽到的维度数 ≥ N（发现阶段一个维度都还没抽） |
| 命中/排除某词 | 发现阶段 | 词面匹配 |
| 属于 direct/alternative/adjacent 层 | **不可执行** | 需要语义判断——记进 ``not_executable``，不假装执行了 |

解析不出来的条款**不能静默忽略**（那等于让报告里的口径变成谎言）：
规则里带 ``source``（model/partial/unparsed/default）与 ``not_executable``，
让报告与前端能说清"哪条没被执行、为什么"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.workflow.site_types import SITE_PRODUCT, classify_site, site_type_label

# 默认口径里的维度门槛（与 `DEFAULT_ADMISSION_RULE` 一致）
DEFAULT_MIN_DIMENSIONS = 2

# 层名的中英折叠（模型会写「直接层」，枚举是 direct）
_LAYER_WORDS = {
    "直接层": "direct", "直接": "direct", "direct": "direct",
    "替代层": "alternative", "替代": "alternative", "alternative": "alternative",
    "邻近层": "adjacent", "邻近": "adjacent", "adjacent": "adjacent",
}

# 条款分隔符：分号、句号、换行、逗号、顿号、加号、"并且/而且"
_CLAUSE_SPLIT = re.compile(r"[；;。\n]|[，,、]|\+|并且|而且")

_SITE_NEGATED = re.compile(r"(不需要|无需|不要求|不必|可以没有)\s*(有)?\s*(独立|官方)?\s*官网")
_SITE_REQUIRED = re.compile(r"(必须|须|应该|应当|应|需要)?\s*(有|具备|拥有|提供|带)?\s*(独立|官方)?\s*官网")
_MIN_DIM_BEFORE = re.compile(
    r"(?:至少|不少于|不低于|≥|>=|大于)\s*(?:有|得|到)?\s*(\d+)\s*个?\s*(?:维度|维)"
)
_MIN_DIM_AFTER = re.compile(r"(\d+)\s*个?\s*(?:维度|维)\s*(?:以上|及以上|起|或更多)")
_LAYER_TOKEN = re.compile(r"(直接层|替代层|邻近层|direct|alternative|adjacent)", re.IGNORECASE)

# 「同赛道」类条款：现在**可执行**了（名册 + 赛道词 + 跨行业词）
_SAME_TRACK_TOKEN = re.compile(
    r"(同赛道|同一赛道|同类目|同类产品|同品类|同类型|同客群|同场景|同领域|"
    r"属于本赛道|同类竞品|same\s+track)", re.IGNORECASE
)


@dataclass
class AdmissionRule:
    """一条**可执行**的准入规则。"""

    # 发现阶段能判的
    require_product_site: bool = True
    must_include: List[str] = field(default_factory=list)
    must_exclude: List[str] = field(default_factory=list)
    # 同赛道（名册 + 赛道词 + 跨行业词，见 src/workflow/track_scope.py）
    require_same_track: bool = False
    # 抓后阶段才判的（发现阶段拿不到）
    min_dimensions: int = DEFAULT_MIN_DIMENSIONS
    # 不可执行的（层归属要语义判断）
    layers: List[str] = field(default_factory=list)
    # 记账
    source: str = "default"          # model / partial / unparsed / default
    text: str = ""
    executable: List[str] = field(default_factory=list)
    not_executable: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "require_product_site": self.require_product_site,
            "min_dimensions": self.min_dimensions,
            # 维度门槛在抓后阶段执行——写清楚阶段，别让人以为发现阶段就判了
            "min_dimensions_stage": "post_crawl",
            "layers": list(self.layers),
            "must_include": list(self.must_include),
            "must_exclude": list(self.must_exclude),
            "require_same_track": self.require_same_track,
            "executable": list(self.executable),
            "not_executable": list(self.not_executable),
        }


def _split_clauses(text: str) -> List[str]:
    parts = [p.strip(" 　·•-—") for p in _CLAUSE_SPLIT.split(text or "")]
    return [p for p in parts if p]


def parse_admission_rule(
    text: str,
    criteria: Optional[Dict[str, Any]] = None,
) -> AdmissionRule:
    """把模型写的准入标准解析成可执行规则。

    解析不出可执行条款时（``source="unparsed"``）退回默认口径：
    必须有官网 + 至少 2 个维度有数据。退回这件事要**留在记录里**，
    不能让报告继续显示那句没人执行的自然语言。
    """
    criteria = criteria or {}
    raw = str(text or "").strip()
    rule = AdmissionRule(
        text=raw,
        must_include=[str(t).strip() for t in (criteria.get("must_include") or []) if str(t).strip()],
        must_exclude=[str(t).strip() for t in (criteria.get("must_exclude") or []) if str(t).strip()],
    )

    clauses = _split_clauses(raw)
    executable_clauses: List[str] = []
    not_executable_clauses: List[str] = []
    saw_site = False
    saw_dimension = False
    saw_layer = False
    saw_same_track = False

    for clause in clauses:
        hit = False
        # 0) 同赛道要求（名册/赛道词/跨行业词 → 可执行）
        if _SAME_TRACK_TOKEN.search(clause):
            rule.require_same_track = True
            saw_same_track = True
            executable_clauses.append(f"{clause}（按赛道名册 + 赛道词 + 跨行业词判定）")
            hit = True
        # 1) 官网要求
        if _SITE_NEGATED.search(clause):
            rule.require_product_site = False
            executable_clauses.append(f"{clause}（不要求官网）")
            hit = True
        elif _SITE_REQUIRED.search(clause):
            rule.require_product_site = True
            saw_site = True
            executable_clauses.append(f"{clause}（必须是产品官网）")
            hit = True
        # 2) 维度门槛
        dim_match = _MIN_DIM_BEFORE.search(clause) or _MIN_DIM_AFTER.search(clause)
        if dim_match:
            rule.min_dimensions = max(1, int(dim_match.group(1)))
            saw_dimension = True
            executable_clauses.append(f"{clause}（抓后判定：维度数 ≥ {rule.min_dimensions}）")
            hit = True
        # 3) 层归属：认得出层名，但**判不了**（要语义判断）
        layer_match = _LAYER_TOKEN.search(clause)
        if layer_match:
            word = layer_match.group(1).lower()
            layer = _LAYER_WORDS.get(word) or _LAYER_WORDS.get(layer_match.group(1))
            if layer and layer not in rule.layers:
                rule.layers.append(layer)
            saw_layer = True
            not_executable_clauses.append(
                f"{clause}（层归属要语义判断，代码不据此裁决）"
            )
            hit = True
        if not hit:
            not_executable_clauses.append(f"{clause}（无法转成可执行条件）")

    # 结构化的 include/exclude 一直是可执行的
    if rule.must_include:
        executable_clauses.append("必须包含：" + "、".join(rule.must_include))
    if rule.must_exclude:
        executable_clauses.append("必须排除：" + "、".join(rule.must_exclude))

    if not executable_clauses:
        # 一条都解析不出来 → 退回默认口径，并**如实标记**
        rule.source = "unparsed"
        rule.require_product_site = True
        rule.min_dimensions = DEFAULT_MIN_DIMENSIONS
        rule.executable = [
            "必须有官网（默认口径）",
            f"至少 {rule.min_dimensions} 个维度有数据（抓后判定，默认口径）",
        ]
        rule.not_executable = not_executable_clauses or ([raw] if raw else [])
        return rule

    rule.executable = executable_clauses
    rule.not_executable = not_executable_clauses
    if not_executable_clauses:
        rule.source = "partial"
    else:
        rule.source = "model"
    # 没提官网的条款：默认口径要求有官网（写进 executable，别默默执行）
    if not saw_site:
        rule.executable.insert(0, "必须有官网（默认口径，规则未另行说明）")
    if not saw_dimension:
        rule.executable.append(
            f"至少 {rule.min_dimensions} 个维度有数据（抓后判定，默认口径）"
        )
    return rule


def evaluate_discovery(
    candidate: Dict[str, Any],
    rule: AdmissionRule,
    track_keywords: Sequence[str] = (),
) -> Tuple[bool, List[str]]:
    """发现阶段能执行的门槛：站点类型 + 排除项 + （可选）同赛道。

    返回 (是否通过, 理由)。理由会原样进"为什么没入选"的清单——
    用户要能看懂为什么某家没进来。

    `track_keywords` 是**计划里模型给的赛道词**（当轮口径），
    和生产侧的 `assets/track_scope.json` 一起喂给同赛道判据。
    """
    reasons: List[str] = []
    url = str(candidate.get("url") or "")
    name = str(candidate.get("name") or "")
    description = str(candidate.get("description") or "")

    if rule.require_product_site:
        site_type = candidate.get("site_type") or classify_site(url, name)
        if site_type != SITE_PRODUCT:
            reasons.append(f"站点类型是{site_type_label(site_type)}，不是产品官网")

    excluded = [t for t in rule.must_exclude if t and t in (name + description)]
    if excluded:
        reasons.append("命中排除项: " + "、".join(excluded))

    if rule.require_same_track:
        from src.workflow.track_scope import check_same_track

        ok, why = check_same_track(
            name=name,
            url=url,
            description=description,
            extra_keywords=list(track_keywords or []),
        )
        if not ok:
            reasons.append(why)

    return (not reasons), reasons


def evaluate_post_crawl(
    competitor: Dict[str, Any],
    rule: AdmissionRule,
) -> Tuple[bool, List[str]]:
    """抓完之后的门槛：维度数够不够。

    这一条在发现阶段**判不了**（那时一个维度都还没抽），
    所以它被放在抓后阶段执行，而不是被丢掉。
    """
    from src.tools.quality_scorer import QualityScorer

    count = QualityScorer.count_dimensions(competitor)
    if count < rule.min_dimensions:
        return False, [f"只抽到 {count} 个维度，低于口径要求的 {rule.min_dimensions} 个"]
    return True, []
