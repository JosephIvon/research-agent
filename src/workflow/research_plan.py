"""调研意图澄清与研究计划（Clarify & Scope 层）。

解决的问题
----------
用户给的是**任务描述**，不是搜索词：「找一下百度搭子的全部竞品信息」。
今天系统把整句丢给搜索引擎（``search_topics``），取回前 N 个 URL 就当竞品
去抓——于是：不知道「百度搭子」是什么、抓回来的可能不是竞品、没有数量与
质量约束、也没有"该重点看哪几个角度"。

实测：任务句式下，最该抓的"官方介绍页"和"竞品盘点页"会被相关性过滤丢掉
（query 里的"找一下/全部/的/信息"稀释了覆盖率，实体词只占少数），
只有写成「百度搭子 竞品」这种搜索式才留得住。**系统质量取决于用户像不像
在写搜索词**，这是产品级缺陷。

这一层把任务描述翻译成**可校验的研究计划**：主体是谁、竞品怎么定义、
选几家、按什么角度、用什么查询去打。计划先落地，再去抓。

为什么不需要行业规则表
--------------------
和「可借鉴点」那一课同一个道理：难点不是行业知识，是**结构**。
- 实体类型 = 有限枚举（app/平台/硬件/服务/品牌/公司/组织/概念…），模型判
- 竞品定义 = 「同客群 × 同场景 × 同交付形态」的结构化判定，模型产出、代码校验
- 角度权重 = **产品默认值**写在 config，模型只给优先级排序，数值由代码算
- 候选打分 = 证据量 / 数据完整度 / 与主体相似度，都是结构化指标

没有任何一项需要"这个行业该有哪些竞品"的知识表。

权重机制：不用"扮演角色"直接调权重
----------------------------------
角色扮演调权重不可见、不可测、不可复现——断言不了"研发视角下权重是 .2"，
eval 里也量不出来。同一份表达力可以做得可观测：
- 无角色/无意图线索 → 直接用默认权重表
- 有角色（"偏研发/技术选型"）→ 模型**只输出角度优先级 + 理由**，不输出数字
  → 代码按优先级映射（primary×1.5 / secondary×1.0 / ignore×0.5）后归一化
模型做判断，代码算数；结果落 state，可读、可测、可复现。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.agent.citations import make_evidence_id
from src.workflow.admission_rules import evaluate_discovery, parse_admission_rule
from src.workflow.site_types import SITE_PRODUCT, classify_site, site_type_label

PLAN_INTENT = "plan"
PLAN_MAX_TOKENS = 16384

# ---- 有限枚举（结构，不是行业知识）------------------------------------
ENTITY_TYPES = (
    "app", "platform", "software", "hardware", "service", "brand",
    "company", "organization", "concept", "person", "place", "unknown",
)

# 模型会**改写**枚举词（实测真实模型给过 entity_type="product"、
# angle="pricing/market_positioning/technology"）。枚举是**契约的词汇表**，
# 不是判决：改写就折叠回规范词，而不是整份拒收——模型给出的主体判断与
# 竞品口径仍然有用，为一个同义词把整份计划扔掉是最亏的。
# 折叠表是**通用词形映射**（与行业无关）。
_ENTITY_TYPE_SYNONYMS: Dict[str, str] = {
    "app": "app", "application": "app", "mobile_app": "app", "mobile app": "app",
    "小程序": "app", "应用": "app", "手机应用": "app", "软件应用": "app",
    "platform": "platform", "website": "platform", "web": "platform",
    "site": "platform", "saas": "platform", "平台": "platform", "网站": "platform",
    "software": "software", "tool": "software", "系统": "software",
    "软件": "software", "工具": "software", "工具软件": "software",
    "hardware": "hardware", "device": "hardware", "硬件": "hardware", "设备": "hardware",
    "service": "service", "服务": "service", "服务类": "service",
    "brand": "brand", "品牌": "brand", "牌子": "brand",
    "company": "company", "enterprise": "company", "vendor": "company",
    "公司": "company", "企业": "company", "厂商": "company",
    "organization": "organization", "org": "organization", "机构": "organization",
    "组织": "organization", "平台方": "organization",
    "concept": "concept", "概念": "concept", "概念级": "concept",
    "person": "person", "人物": "person", "人": "person",
    "place": "place", "地点": "place", "城市": "place", "地区": "place",
}

_ANGLE_SYNONYMS: Dict[str, str] = {
    "feature": "feature", "features": "feature", "function": "feature",
    "functions": "feature", "capability": "feature", "capabilities": "feature",
    "功能": "feature", "特性": "feature", "功能特性": "feature", "能力": "feature",
    "business": "business", "business_model": "business", "business value": "business",
    "pricing": "business", "price": "business", "revenue": "business",
    "monetization": "business", "market": "business", "market_positioning": "business",
    "gtm": "business", "go_to_market": "business", "commercial": "business",
    "商业模式": "business", "定价": "business", "商业": "business", "市场": "business",
    "工程": "engineering", "engineering": "engineering", "technology": "engineering",
    "tech": "engineering", "technical": "engineering", "architecture": "engineering",
    "implementation": "engineering", "技术": "engineering", "研发": "engineering",
    "架构": "engineering", "技术实现": "engineering",
    "product": "product", "ux": "product", "ui": "product",
    "experience": "product", "usability": "product", "产品": "product",
    "体验": "product", "交互": "product", "易用": "product",
}

# ---- 分析角度与默认权重（产品默认值）---------------------------------
DEFAULT_ANGLES: Tuple[Tuple[str, str, float], ...] = (
    ("feature", "功能", 0.35),
    ("business", "业务价值", 0.30),
    ("engineering", "研发与实现", 0.20),
    ("product", "产品与体验", 0.15),
)
ANGLE_LABELS = {key: label for key, label, _ in DEFAULT_ANGLES}
ANGLE_ORDER = [key for key, _, _ in DEFAULT_ANGLES]

# 优先级 → 权重倍率（数值由代码算，不由模型给）
PRIORITY_MULTIPLIER = {"primary": 1.5, "secondary": 1.0, "ignore": 0.5}

# ---- 默认约束 --------------------------------------------------------
DEFAULT_MIN_COMPETITORS = 3
DEFAULT_MAX_COMPETITORS = 5
DEFAULT_CANDIDATE_POOL = 10   # 候选池先抓 8–12，取打分前 N
DEFAULT_ADMISSION_RULE = "必须有官网 + 至少 2 个维度有数据"
# 入选分数线：0.5 = "像产品自己的官网"。发现阶段没有维度数据可看，
# 门槛只能定在**发现阶段拿得到**的那一层（站点类型 + 词面相关）。
MIN_ADMISSION_SCORE = 0.5
COMPETITOR_LAYERS = ("direct", "alternative", "adjacent")
DEFAULT_LAYERS = ("direct",)

# ---- 搜索四段式 ------------------------------------------------------
SEARCH_STAGES = ("entity_anchor", "competitor_discovery", "per_competitor_verify", "counter_evidence")

# 阶段名的同义改写（实测：模型会写 "discovery"/"competitor_search"/"verify"）。
# 与角度同义折叠一个道理：**改写不是错误**，为它把整份计划扔掉最亏；
# 但阶段名错了是真错——认不出来就查不到东西（实测：阶段名不匹配 → 0 条查询 →
# 候选池 0 家，整轮空转且看不出来）。
_STAGE_SYNONYMS = {
    "entity_anchor": ("anchor", "entity", "subject", "entity_resolution", "identity",
                      "主体锚定", "主体确认", "主体识别"),
    "competitor_discovery": ("discovery", "competitor_search", "competitors",
                             "find_competitors", "competitor_candidates", "candidate_pool",
                             "竞品发现", "竞品检索", "竞品候选"),
    "per_competitor_verify": ("verify", "verification", "competitor_verify",
                               "per_competitor", "detail", "verify_competitors",
                               "逐家验证", "竞品验证"),
    "counter_evidence": ("counter", "negative_evidence", "critique", "counterpoints",
                         "risks", "complaints", "反证", "反面证据"),
}


def fold_stage(name: str) -> Optional[str]:
    """把阶段名折叠回四个规范阶段之一；认不出来返回 None。"""
    raw = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return None
    if raw in SEARCH_STAGES:
        return raw
    for canonical, aliases in _STAGE_SYNONYMS.items():
        if raw == canonical:
            return canonical
        for alias in aliases:
            if raw == alias.lower() or raw in alias.lower() or alias.lower() in raw:
                return canonical
    return None
STAGE_LABELS = {
    "entity_anchor": "实体锚定",
    "competitor_discovery": "竞品发现",
    "per_competitor_verify": "逐家验证",
    "counter_evidence": "反证",
}

_SYSTEM_PROMPT = (
    "<!--intent:" + PLAN_INTENT + "-->\n"
    "你是竞品研究的**规划员**。把用户的调研请求翻译成一份结构化研究计划。\n\n"
    "规则：\n"
    "1. 用户给的是**任务描述**，不是搜索词。先弄清主体是什么："
    "entity.what_it_is 用一句话说明主体是做什么的；"
    "entity.entity_type 只能取 "
    + " / ".join(ENTITY_TYPES) + " 之一。\n"
    "2. 若主体名有歧义（同名不同物），列出 disambiguation_candidates，"
    "resolved_confidence 给 0–1 的置信度，不要假装确定。"
    "如果知道主体自己的官网域名，写进 entity.official_domains（可为空数组）——"
    "主体的产品名常常和用户说的名字不一样（如「百度搭子」= dumate.cn），"
    "有域名才能把它从竞品里排掉。\n"
    "3. competitor_criteria 给出竞品的判定标准（写「同客群×同场景×同交付形态」"
    "这类可判定条件），layers 只能取 direct/alternative/adjacent，"
    "count 落在 2–8 之间。\n"
    "3b. competitor_candidates 列出你**知道的**、可能与主体同赛道的竞品名称"
    "（最多 8 个，拿不准的也可以列）。它们只是**候选**：系统会自己去搜它的官网、"
    "判断是不是真的产品官网，**你说了不算数**。但这一步很有用——"
    "搜索框里搜「猫箱 竞品」回来的全是榜单文章，而你其实知道同赛道是谁"
    "（星野、筑梦岛、Talkie、Character.AI）。\n"
    "4. angles：分析角度的**优先级**（primary/secondary/ignore）、理由，"
    "以及该角度值得看的**站内页面关键词** page_hints（如定价页、功能页、API 文档）。"
    "angle 只能取 " + " / ".join(ANGLE_ORDER) + "（"
    + " / ".join(f"{k}={v}" for k, v in ANGLE_LABELS.items()) + "）。"
    "**不要输出权重数字**——权重由系统按优先级计算。"
    "若用户表达了角色或侧重（如「偏研发」「站在采购角度」），"
    "据此排优先级；没有这类线索就四个角度都标 secondary。\n"
    "5. search_plan 按四个阶段给查询词：entity_anchor（弄清主体是什么）、"
    "competitor_discovery（找候选竞品）、per_competitor_verify（逐家查证）、"
    "counter_evidence（找缺点与评价）。查询词要短，是**搜索词**不是任务句。\n"
    "5a. **entity_anchor 与 competitor_discovery 这两个阶段的查询词不用你写**"
    "（系统按固定模板生成，写了也会被覆盖）——写这两个阶段只是为了占位。"
    "你只需要给 per_competitor_verify 与 counter_evidence 两个阶段的查询词，"
    "它们是**模板**：写成带占位符的形式，如「{竞品名} 定价」「{竞品名} 官网 功能」，"
    "系统会把真竞品名填进去。\n"
    "5b. **per_competitor_verify / counter_evidence 的查询词要能直接命中产品官网**。"
    "实测（2026-10-28）：品类级的对比/排行词（'AI陪伴 应用 对比'、'AI陪伴 应用 排行'）"
    "搜回来的全是榜单文章与工具目录，入选名单一家真竞品都没有。"
    "要写成能落到**某个产品首页**的形式："
    "'<品类> App 官网'、'<品类> 下载'、'<已知竞品名>'、'<品类> 产品 官网'。"
    "主体是中文产品时，查询词也用中文（英文词会搜回英文工具目录站）。\n"
    "6. open_questions 列出需要用户确认的点（带 default 默认值），"
    "没有就不列。\n"
    "7. **长度上限**（超了会被截断，整份计划就废了）："
    "what_it_is ≤ 60 字；admission_rule ≤ 150 字；"
    "must_include / must_exclude 各 ≤ 3 条、每条 ≤ 60 字；"
    "reason ≤ 50 字；page_hints ≤ 8 个、每个 ≤ 12 字；"
    "每阶段 queries ≤ 4 条、每条 ≤ 20 字。\n\n"
    "输出严格 JSON，不要 markdown 代码块：\n"
    '{"entity":{"name":"","entity_type":"","what_it_is":"",'
    '"disambiguation_candidates":[],"resolved_confidence":0.0,'
    '"official_domains":[]},'
    '"competitor_criteria":{"layers":["direct"],"must_include":[],'
    '"must_exclude":[],"min_count":3,"max_count":5,"admission_rule":""},'
    '"competitor_candidates":[],'
    '"angles":[{"angle":"feature","priority":"secondary","reason":"",'
    '"page_hints":["定价","功能"]}],'
    '"search_plan":[{"stage":"entity_anchor","queries":[""],"acceptance":""}],'
    '"open_questions":[{"question":"","default":""}]}\n'
)


@dataclass
class Angle:
    """一个分析角度。权重由代码按优先级算，不由模型给。"""

    angle: str
    priority: str = "secondary"          # primary / secondary / ignore
    reason: str = ""
    weight: float = 0.0
    # 该角度值得看的**站内页面关键词**（模型给，代码用作选页提示）
    page_hints: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "angle": self.angle,
            "label": ANGLE_LABELS.get(self.angle, self.angle),
            "priority": self.priority,
            "reason": self.reason,
            "weight": round(self.weight, 4),
            "page_hints": list(self.page_hints),
        }


@dataclass
class SearchStage:
    """一个查询规划阶段。"""

    stage: str
    queries: List[str] = field(default_factory=list)
    acceptance: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "label": STAGE_LABELS.get(self.stage, self.stage),
            "queries": list(self.queries),
            "acceptance": self.acceptance,
        }


@dataclass
class ResearchPlan:
    """一份可校验的研究计划。"""

    entity_name: str = ""
    entity_type: str = "unknown"
    what_it_is: str = ""
    disambiguation_candidates: List[str] = field(default_factory=list)
    resolved_confidence: float = 0.0
    # 主体自己的官网域名（模型给的，可为空）。主体的产品名常与用户说法不同
    # （"百度搭子" = dumate.cn），有域名才能把主体从竞品里排掉。
    official_domains: List[str] = field(default_factory=list)
    criteria: Dict[str, Any] = field(default_factory=dict)
    # 模型**提名**的竞品（只是候选，代码会搜它的官网并核验是不是产品官网）。
    # 作用：搜索框搜"猫箱 竞品"回来的全是榜单文章，而模型其实知道同赛道是谁。
    competitor_candidates: List[str] = field(default_factory=list)
    # 准入规则的**可执行形态**（从 criteria.admission_rule 解析而来）：
    # 报告里写的那句话，到底哪几条被代码判了、在哪个阶段判、哪条判不了。
    admission_rule: Dict[str, Any] = field(default_factory=dict)
    angles: List[Angle] = field(default_factory=list)
    search_plan: List[SearchStage] = field(default_factory=list)
    open_questions: List[Dict[str, str]] = field(default_factory=list)
    source: str = "default"          # default / model / fallback
    error: str = ""

    # -- 便捷视图 --------------------------------------------------------
    @property
    def layers(self) -> List[str]:
        layers = self.criteria.get("layers") or []
        return [l for l in layers if l in COMPETITOR_LAYERS]

    @property
    def min_count(self) -> int:
        return int(self.criteria.get("min_count") or DEFAULT_MIN_COMPETITORS)

    @property
    def max_count(self) -> int:
        return int(self.criteria.get("max_count") or DEFAULT_MAX_COMPETITORS)

    @property
    def candidate_pool(self) -> int:
        return int(self.criteria.get("candidate_pool") or DEFAULT_CANDIDATE_POOL)

    @property
    def angle_weights(self) -> Dict[str, float]:
        return {a.angle: a.weight for a in self.angles}

    @property
    def top_angles(self) -> List[str]:
        """权重最高的两个角度——评测"角度覆盖度"的靶子。"""
        ordered = sorted(self.angles, key=lambda a: -a.weight)
        return [a.angle for a in ordered[:2]]

    def stage(self, name: str) -> Optional[SearchStage]:
        for stage in self.search_plan:
            if stage.stage == name:
                return stage
        return None

    def queries_for(self, name: str) -> List[str]:
        stage = self.stage(name)
        return list(stage.queries) if stage else []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity": {
                "name": self.entity_name,
                "entity_type": self.entity_type,
                "what_it_is": self.what_it_is,
                "disambiguation_candidates": list(self.disambiguation_candidates),
                "resolved_confidence": round(self.resolved_confidence, 3),
                "official_domains": list(self.official_domains),
            },
            "competitor_criteria": dict(self.criteria),
            "competitor_candidates": list(self.competitor_candidates),
            # 可执行形态的准入规则：报告/前端要能说清"这句口径哪几条真的判了"
            "admission_rule": dict(self.admission_rule),
            "angles": [a.to_dict() for a in self.angles],
            "search_plan": [s.to_dict() for s in self.search_plan],
            "open_questions": list(self.open_questions),
            "source": self.source,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResearchPlan":
        """从 ``to_dict`` 的结果还原（ReAct 工具要用 workspace 里的计划）。

        还原的是**已校验过的**计划：权重照抄（它本来就是代码算的），
        不重新走一遍模型。
        """
        data = data or {}
        entity = data.get("entity") or {}
        criteria = data.get("competitor_criteria") or {}
        angles = []
        for item in data.get("angles") or []:
            if not isinstance(item, dict):
                continue
            angles.append(Angle(
                angle=str(item.get("angle") or ""),
                priority=str(item.get("priority") or "secondary"),
                reason=str(item.get("reason") or ""),
                weight=float(item.get("weight") or 0.0),
                page_hints=[str(h) for h in (item.get("page_hints") or [])],
            ))
        stages = []
        for item in data.get("search_plan") or []:
            if not isinstance(item, dict):
                continue
            stages.append(SearchStage(
                stage=str(item.get("stage") or ""),
                queries=[str(q) for q in (item.get("queries") or [])],
                acceptance=str(item.get("acceptance") or ""),
            ))
        return cls(
            entity_name=str(entity.get("name") or ""),
            entity_type=str(entity.get("entity_type") or "unknown"),
            what_it_is=str(entity.get("what_it_is") or ""),
            disambiguation_candidates=[str(c) for c in (entity.get("disambiguation_candidates") or [])],
            resolved_confidence=float(entity.get("resolved_confidence") or 0.0),
            official_domains=[
                str(d).strip().lower().lstrip(".")
                for d in (entity.get("official_domains") or []) if str(d).strip()
            ],
            criteria=dict(criteria),
            competitor_candidates=[
                str(c).strip() for c in (data.get("competitor_candidates") or [])
                if str(c).strip()
            ][:12],
            angles=angles,
            search_plan=stages,
            open_questions=list(data.get("open_questions") or []),
            source=str(data.get("source") or "default"),
            error=str(data.get("error") or ""),
        )


# ----------------------------------------------------------------------
# 权重：数值一律由代码算
# ----------------------------------------------------------------------

def fold_angle(name: Any) -> Optional[str]:
    """把模型给的角度名折叠回规范角度；认不出返回 None（丢弃该角度）。"""
    key = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return None
    if key in ANGLE_ORDER:
        return key
    if key in _ANGLE_SYNONYMS:
        return _ANGLE_SYNONYMS[key]
    compact = key.replace("_", "")
    for candidate, canonical in _ANGLE_SYNONYMS.items():
        if candidate.replace("_", "") == compact:
            return canonical
    return None


def fold_entity_type(name: Any) -> str:
    """把模型给的实体类型折叠回规范枚举；认不出就是 unknown（不拒收）。"""
    key = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        return "unknown"
    if key in ENTITY_TYPES:
        return key
    if key in _ENTITY_TYPE_SYNONYMS:
        return _ENTITY_TYPE_SYNONYMS[key]
    compact = key.replace("_", "")
    for candidate, canonical in _ENTITY_TYPE_SYNONYMS.items():
        if candidate.replace("_", "") == compact:
            return canonical
    return "unknown"


def fold_priority(value: Any) -> str:
    """优先级折叠：认不出一律 secondary（中性，不猜）。"""
    key = str(value or "").strip().lower()
    if key in PRIORITY_MULTIPLIER:
        return key
    if key in ("high", "most_important", "top", "核心", "高"):
        return "primary"
    if key in ("low", "none", "skip", "不重要", "低"):
        return "ignore"
    return "secondary"


def resolve_weights(priorities: Dict[str, str]) -> Dict[str, float]:
    """按优先级倍率算权重并归一化；没提到的角度用 secondary。

    没有优先级输入 → 直接用默认权重表（产品默认值，不猜）。
    """
    if not priorities:
        return {key: weight for key, _, weight in DEFAULT_ANGLES}
    raw: Dict[str, float] = {}
    for key, _, default_weight in DEFAULT_ANGLES:
        priority = priorities.get(key, "secondary")
        multiplier = PRIORITY_MULTIPLIER.get(priority, 1.0)
        raw[key] = default_weight * multiplier
    total = sum(raw.values()) or 1.0
    return {key: value / total for key, value in raw.items()}


def build_default_angles() -> List[Angle]:
    return [
        Angle(angle=key, priority="secondary", reason="默认权重（用户未指定角色或侧重）",
              weight=weight)
        for key, _, weight in DEFAULT_ANGLES
    ]


def angle_priority_tiers(plan: ResearchPlan) -> Optional[Tuple[Tuple[int, Tuple[str, ...]], ...]]:
    """把角度权重翻译成抓取器的 ``priority_tiers``（高权重角度的页面先抓）。

    复用抓取器已有的选页机制（``discover_research_links(priority_tiers=...)``）：
    角度权重不新造轮子，只决定**先看哪些页面**。权重是 0–1 的小数，
    这里放大成整数档位（1–100），保证排序稳定。
    """
    tiers: List[Tuple[int, Tuple[str, ...]]] = []
    for angle in plan.angles:
        hints = tuple(h for h in angle.page_hints if h)
        if not hints:
            continue
        tiers.append((max(1, int(round(angle.weight * 100))), hints))
    if not tiers:
        return None
    tiers.sort(key=lambda item: -item[0])
    return tuple(tiers)


def build_default_plan(user_query: str) -> ResearchPlan:
    """没有模型可用时的兜底计划：默认角度权重 + 默认约束。

    注意：主体名仍然从 query 里取（用户原话），但**不假装知道它是什么**
    ——what_it_is 留空，由后续实体锚定阶段补。
    """
    name = _entity_name_from_query(user_query)
    # 默认查询词：没有模型出计划时，搜索**不能因此没有查询词**。
    # 此前默认计划的 search_plan 是空的 → 竞品发现阶段无查询可跑 →
    # 整段退回"把整句任务描述当搜索词"（实测正是官方介绍页与盘点页被丢、
    # 媒体页被当成竞品的那条路）。
    queries = _default_search_queries(name)
    return ResearchPlan(
        entity_name=name,
        entity_type="unknown",
        what_it_is="",
        criteria={
            "layers": list(DEFAULT_LAYERS),
            "must_include": [],
            "must_exclude": [],
            "min_count": DEFAULT_MIN_COMPETITORS,
            "max_count": DEFAULT_MAX_COMPETITORS,
            "candidate_pool": DEFAULT_CANDIDATE_POOL,
            "admission_rule": DEFAULT_ADMISSION_RULE,
        },
        angles=build_default_angles(),
        search_plan=queries,
        source="default",
    )


def _default_search_queries(entity_name: str) -> List[SearchStage]:
    """默认查询规划：按主体名生成四段式的查询词。

    主体名为空（连主体都没抽出来）时返回空列表——此时退回旧行为也不失真，
    硬编一个查询词反而会去搜"未知实体"。
    """
    name = (entity_name or "").strip()
    if not name:
        return []
    return [
        SearchStage(
            stage="entity_anchor",
            queries=list(_deterministic_queries("entity_anchor", name, "")),
            acceptance="命中主体官方源，用于确认主体是什么",
        ),
        SearchStage(
            stage="competitor_discovery",
            queries=list(_deterministic_queries("competitor_discovery", name, "")),
            acceptance="产出候选池，媒体/百科页会被剔除",
        ),
        SearchStage(
            stage="per_competitor_verify",
            queries=[],
            acceptance="每家至少 2 个维度有数据",
        ),
        SearchStage(
            stage="counter_evidence",
            queries=[],
            acceptance="补齐限制与评价",
        ),
    ]


# ----------------------------------------------------------------------
# 发现阶段的查询词：由代码按模板拼装，不由模型自由发挥
# ----------------------------------------------------------------------
#
# 为什么（实测 2026-10-29，`tool_e2e_plan_variance.py`）：
# 同一主体「猫箱」连生成 5 次调研计划，**查询词两两 Jaccard 只有 0.097**
# （百度搭子 0.125），10 对计划**无一对逐字相同**。模型每次都换写法：
# "猫箱 AI 官网" / "猫箱 官网" / "猫箱 App 官网" / "猫箱 AI 官方"……
#
# 计划查询词每次换 → 搜到的结果集换 → 候选池换 → 入选名单换
# → "同一句式重复跑"的稳定性线（S1）测的是**计划抽签**，不是管线行为；
# 跨句式的 Jaccard（Q3）更是必然为 0。
#
# 口径：**模型提名（主体名、竞品名单、赛道判断），代码拼装（查询词）**。
# 模型仍然决定"找谁"，代码决定"怎么搜"——同输入必得同查询，
# 差异只可能来自搜索后端本身。
#
# 这两个阶段的查询词**不再采信模型输出**（哪怕模型照旧写了也照样覆盖）：
# 依赖提示词"请别写"是软约束，模板拼装才是硬约束。

# 主体类型 → 锚定阶段的第二种问法。写死映射而不是让模型现编，
# 是为了"同一主体每次问法一样"。
_ANCHOR_TYPE_PHRASE = {
    "app": "App 官网",
    "application": "App 官网",
    "website": "网站",
    "site": "网站",
    "product": "产品官网",
    "service": "服务官网",
    "platform": "平台官网",
}

# 发现阶段的品类级问法（固定两条）。竞品官网的查询词由发现阶段执行器
# 按"<提名竞品名> 官网 / 官方网站"另发（见 search_stages.py），不在这两条里。
_DISCOVERY_TEMPLATES = ("{name} 竞品", "{name} 类似应用")


def _deterministic_queries(stage: str, entity_name: str, entity_type: str) -> List[str]:
    """按固定模板拼装 entity_anchor / competitor_discovery 的查询词。

    同一个 (stage, 主体名, 主体类型) 必得同一组查询词——
    这是"计划不再抽签"的实现方式。
    """
    name = (entity_name or "").strip()
    if not name:
        return []
    if stage == "entity_anchor":
        queries = [f"{name} 官网"]
        phrase = _ANCHOR_TYPE_PHRASE.get((entity_type or "").strip().lower())
        if phrase:
            queries.append(f"{name} {phrase}")
    elif stage == "competitor_discovery":
        queries = [tpl.format(name=name) for tpl in _DISCOVERY_TEMPLATES]
    else:
        return []
    # 去重（主体名本身带"官网"时模板会撞车）
    seen = set()
    unique = []
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            unique.append(query)
    return unique


def _normalize_nominees(raw: Any) -> List[str]:
    """把模型提名的竞品名单整理成**稳定顺序**。

    去重（同名只留一个，大小写不敏感）+ **排序**：提名名单是"模型知识"，
    顺序不该带随机性——发现阶段按这个顺序逐个发查询，顺序变了就等于
    同一份计划换了个跑法（实测：同主体两次提名 4–6 个，顺序每次不同）。
    排序让"同一份名单"在任何一次运行里都按同一条路走。
    """
    seen = set()
    names: List[str] = []
    for item in (raw or []):
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return sorted(names)[:12]


def _apply_deterministic_queries(
    stages: List[SearchStage],
    entity_name: str,
    entity_type: str,
) -> List[SearchStage]:
    """把 entity_anchor / competitor_discovery 两个阶段的查询词换成代码拼装的。

    模型写的这两个阶段的查询词**丢弃**：它们是"计划抽签"的主要来源
    （实测 Jaccard 0.097）。其余阶段（逐家验证、反证）仍用模型的模板，
    因为它们是**按竞品展开的意图词**（"{竞品名} 定价"这类），
    写错了只会少查一个角度，不会把候选池换掉。
    """
    out: List[SearchStage] = []
    seen_stages = set()
    for stage in stages:
        if stage.stage in ("entity_anchor", "competitor_discovery"):
            queries = _deterministic_queries(stage.stage, entity_name, entity_type)
            if queries:
                out.append(SearchStage(
                    stage=stage.stage,
                    queries=queries,
                    acceptance=stage.acceptance,
                ))
                seen_stages.add(stage.stage)
            continue
        out.append(stage)
        seen_stages.add(stage.stage)
    # 模型没给这两个阶段时补上（缺阶段 = 该阶段一条都不搜 = 候选池空）
    fallback_acceptance = {
        "entity_anchor": "命中主体官方源，用于确认主体是什么",
        "competitor_discovery": "产出候选池，媒体/百科页会被剔除",
    }
    for stage_name in ("entity_anchor", "competitor_discovery"):
        if stage_name in seen_stages:
            continue
        queries = _deterministic_queries(stage_name, entity_name, entity_type)
        if not queries:
            continue
        out.append(SearchStage(
            stage=stage_name,
            queries=queries,
            acceptance=fallback_acceptance[stage_name],
        ))
    return out


def _entity_name_from_query(user_query: str) -> str:
    """从任务句里粗提主体名（去动作词与修饰）。

    只在**模型不可用**时用；有模型时以模型消歧结果为准。
    尾部修饰要**反复剥**：「百度搭子的全部竞品信息」去掉「竞品信息」后还剩
    「百度搭子的全部」，得再剥一次才是「百度搭子」。
    """
    text = (user_query or "").strip()
    text = re.sub(
        r"^(请)?(帮我|帮忙)?(找一下|查找|搜一下|搜下|调研|研究|分析|对比|调查|"
        r"了解一下|看看)\s*",
        "",
        text,
    )
    tail = re.compile(
        r"(的)?(全部|所有|相关|竞品|竞争对手|同类|类似|产品|公司|平台|信息|资料|情况|"
        r"的)(信息|资料|情况)?[。！!？?]*$"
    )
    previous = None
    while previous != text:
        previous = text
        text = tail.sub("", text).strip()
    # "X的A与B" 这类定语从句：主体多半是 "X"（"美菜的仓配模式与定价打法" → "美菜"）。
    # 只在 "的" 前那一段够短且非空时才截——否则 "的" 本身是名字的一部分。
    if "的" in text:
        head = text.split("的", 1)[0].strip()
        if head and len(head) <= 20:
            text = head
    text = text.strip("。！!？? 　")
    return text[:60]


# ----------------------------------------------------------------------
# 解析与校验
# ----------------------------------------------------------------------

def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    return text


def parse_plan(response: Any) -> Dict[str, Any]:
    text = response if isinstance(response, str) else str(response or "")
    text = _strip_fence(text)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("研究计划响应不是 JSON")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("研究计划响应不是对象")
    return data


def validate_plan_data(data: Dict[str, Any]) -> List[str]:
    """结构校验：返回问题清单（空 = 通过）。不替模型补，也不替它猜。"""
    problems: List[str] = []
    entity = data.get("entity") or {}
    if not isinstance(entity, dict):
        return ["entity 不是对象"]
    # entity.name 缺失不算致命：主体名可以从用户原话里补（属于**修复**，
    # 不是**采纳半份计划**——结构性问题仍然整份拒收）。
    # entity_type 也不因"不在枚举里"而拒收：枚举词会被模型改写，
    # 折叠回规范词即可（fold_entity_type），折叠不出就是 unknown。

    criteria = data.get("competitor_criteria") or {}
    if not isinstance(criteria, dict):
        problems.append("competitor_criteria 不是对象")
    else:
        layers = criteria.get("layers") or []
        if not isinstance(layers, list) or not layers:
            problems.append("competitor_criteria.layers 为空")
        else:
            for layer in layers:
                if layer not in COMPETITOR_LAYERS:
                    problems.append(f"竞品层级非法: {layer}")
        for key in ("min_count", "max_count"):
            value = criteria.get(key)
            if not isinstance(value, int) or not (2 <= value <= 8):
                problems.append(f"competitor_criteria.{key} 非法: {value}")
        min_count = criteria.get("min_count")
        max_count = criteria.get("max_count")
        if isinstance(min_count, int) and isinstance(max_count, int) and min_count > max_count:
            problems.append("competitor_criteria.min_count > max_count")

    angles = data.get("angles") or []
    if not isinstance(angles, list) or not angles:
        problems.append("angles 为空")
    else:
        # 角度名与优先级都会**折叠**（fold_angle / fold_priority）：
        # 改写不是错误，是同义词。真正的问题是"折叠后一个规范角度都不剩"，
        # 那说明模型在说完全不同的事——那种情况退回默认计划更诚实。
        folded = [fold_angle(item.get("angle")) for item in angles if isinstance(item, dict)]
        if not any(folded):
            problems.append("分析角度无法归入任一规范角度")

    stages = data.get("search_plan") or []
    if isinstance(stages, list):
        for stage in stages:
            if not isinstance(stage, dict):
                problems.append("search_plan 元素不是对象")
                continue
            name = str(stage.get("stage") or "")
            # 阶段名能折叠回规范阶段就不是错（同义改写，代码会折），
            # 只有连折叠都认不出来才算非法。
            if fold_stage(name) is None:
                problems.append(f"搜索阶段非法: {name}")
    return problems


def build_plan_from_data(data: Dict[str, Any], user_query: str) -> ResearchPlan:
    """把模型输出组装成 ResearchPlan（权重在这里由代码算）。"""
    entity = data.get("entity") or {}
    criteria = dict(data.get("competitor_criteria") or {})

    # 约束兜底：模型漏了/写错了，代码用默认值补，不让它把范围带跑
    layers = [l for l in (criteria.get("layers") or []) if l in COMPETITOR_LAYERS] or list(DEFAULT_LAYERS)
    min_count = criteria.get("min_count")
    max_count = criteria.get("max_count")
    if not isinstance(min_count, int) or not (2 <= min_count <= 8):
        min_count = DEFAULT_MIN_COMPETITORS
    if not isinstance(max_count, int) or not (2 <= max_count <= 8):
        max_count = DEFAULT_MAX_COMPETITORS
    if min_count > max_count:
        min_count, max_count = max_count, min_count
    criteria.update({
        "layers": layers,
        "min_count": min_count,
        "max_count": max_count,
        "candidate_pool": criteria.get("candidate_pool") or DEFAULT_CANDIDATE_POOL,
        "admission_rule": criteria.get("admission_rule") or DEFAULT_ADMISSION_RULE,
        "must_include": list(criteria.get("must_include") or []),
        "must_exclude": list(criteria.get("must_exclude") or []),
    })

    # 角度：模型给的是**优先级 + 理由 + 页面提示**，权重由代码算。
    # 角度名与优先级先折叠回规范形式：模型改写枚举词（"pricing"、
    # "market_positioning"）是同义改写，不是错误——为它把整份计划扔掉最亏。
    # 多个模型角度折到同一个规范角度时，取**优先级最高**的那个。
    priorities: Dict[str, str] = {}
    reasons: Dict[str, str] = {}
    hints: Dict[str, List[str]] = {}
    priority_rank = {"ignore": 0, "secondary": 1, "primary": 2}
    for item in data.get("angles") or []:
        if not isinstance(item, dict):
            continue
        key = fold_angle(item.get("angle"))
        if key is None:
            continue
        priority = fold_priority(item.get("priority"))
        if key in priorities and priority_rank[priority] <= priority_rank[priorities[key]]:
            continue
        priorities[key] = priority
        reasons[key] = str(item.get("reason") or "")
        hints[key] = [
            str(h).strip() for h in (item.get("page_hints") or [])
            if isinstance(h, str) and h.strip()
        ]
    weights = resolve_weights(priorities)
    angles = [
        Angle(
            angle=key,
            priority=priorities.get(key, "secondary"),
            reason=reasons.get(key, ""),
            weight=weights[key],
            page_hints=hints.get(key, []),
        )
        for key in ANGLE_ORDER
    ]

    stages: List[SearchStage] = []
    for item in data.get("search_plan") or []:
        if not isinstance(item, dict):
            continue
        name = fold_stage(item.get("stage"))
        if name is None:
            continue
        queries = [
            str(q).strip() for q in (item.get("queries") or [])
            if isinstance(q, str) and q.strip()
        ]
        stages.append(SearchStage(
            stage=name,
            queries=queries,
            acceptance=str(item.get("acceptance") or ""),
        ))

    # **每个阶段都要有查询词**：模型漏给或阶段名对不上时，用默认查询词补上。
    # 缺查询词的阶段 = 该阶段一条都不搜 = 候选池空 = 整轮空转，
    # 而这种空转从外面看是"搜索正常、就是没竞品"——最难查的一种故障。
    entity_name_for_queries = str(entity.get("name") or "").strip() or _entity_name_from_query(user_query)
    default_stages = {s.stage: s for s in _default_search_queries(entity_name_for_queries)}
    covered = {s.stage for s in stages if s.queries}
    for stage_name in SEARCH_STAGES:
        if stage_name in covered:
            continue
        fallback = default_stages.get(stage_name)
        if not fallback or not fallback.queries:
            continue
        stages.append(SearchStage(
            stage=stage_name,
            queries=list(fallback.queries),
            acceptance=fallback.acceptance,
        ))

    # **发现阶段的查询词由代码拼装**（2026-10-29）：模型每次生成的查询词都不一样
    # （实测 Jaccard 0.097），会把候选池整个换掉——稳定性线测到的就成了
    # "计划抽签"而不是管线行为。模型仍然决定"找谁"，代码决定"怎么搜"。
    stages = _apply_deterministic_queries(
        stages,
        entity_name_for_queries,
        fold_entity_type(entity.get("entity_type")),
    )

    questions = [
        {"question": str(q.get("question") or ""), "default": str(q.get("default") or "")}
        for q in (data.get("open_questions") or [])
        if isinstance(q, dict) and str(q.get("question") or "").strip()
    ]

    try:
        confidence = float(entity.get("resolved_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return ResearchPlan(
        entity_name=str(entity.get("name") or "").strip() or _entity_name_from_query(user_query),
        entity_type=fold_entity_type(entity.get("entity_type")),
        what_it_is=str(entity.get("what_it_is") or "").strip(),
        disambiguation_candidates=[
            _candidate_label(c) for c in (entity.get("disambiguation_candidates") or [])
            if _candidate_label(c)
        ],
        resolved_confidence=confidence,
        official_domains=[
            str(d).strip().lower().lstrip(".")
            for d in (entity.get("official_domains") or [])
            if str(d).strip()
        ],
        criteria=criteria,
        competitor_candidates=_normalize_nominees(data.get("competitor_candidates")),
        # 准入标准从"描述"变成"可执行"：解析不出就标记，别让报告里的口径
        # 变成谎言（声明与执行脱节是这一轮实测里最贵的坑）
        admission_rule=parse_admission_rule(
            criteria.get("admission_rule"), criteria
        ).to_dict(),
        angles=angles,
        search_plan=stages,
        open_questions=questions,
        source="model",
    )


def _candidate_label(candidate: Any) -> str:
    """消歧候选的展示文本。

    模型有时给字符串（"百度App内搭子功能"），有时给**对象**
    （实测：{"name": "百度搭子独立App", "description": ..., "confidence": 0.62}）。
    两种都要变成一行人能读的文本——直接把 dict 转成字符串会得到
    "{'name': '百度搭子独立App', 'description': ...}" 这种噪音。
    """
    if isinstance(candidate, dict):
        name = str(candidate.get("name") or candidate.get("candidate") or "").strip()
        description = str(candidate.get("description") or "").strip()
        confidence = candidate.get("confidence") or candidate.get("likelihood")
        if name and description:
            label = f"{name}（{description}）"
        else:
            label = name or description
        if confidence not in (None, ""):
            try:
                label += f"（置信度 {float(confidence):.2f}）"
            except (TypeError, ValueError):
                pass
        return label
    return str(candidate).strip()


async def build_research_plan(
    llm_client: Any,
    user_query: str,
    self_context: str = "",
) -> ResearchPlan:
    """问模型要一份研究计划；任何异常都退回默认计划（不阻断链路）。"""
    fallback = build_default_plan(user_query)
    user = {
        "调研请求": user_query,
        "我方产品/目标/约束": (self_context or "").strip() or "（未提供）",
    }
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]
    try:
        response = await llm_client.achat_completion(
            messages, temperature=0.2, max_tokens=PLAN_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001
        fallback.error = f"LLM 调用失败: {type(exc).__name__}: {exc}"
        return fallback
    try:
        data = parse_plan(response)
    except Exception as exc:  # noqa: BLE001
        fallback.error = f"计划响应解析失败: {exc}"
        return fallback

    problems = validate_plan_data(data)
    if problems:
        # 结构不合格就不采纳：半份计划比没计划更危险
        fallback.error = "计划结构校验未通过: " + "；".join(problems[:5])
        return fallback
    return build_plan_from_data(data, user_query)


# ----------------------------------------------------------------------
# 候选打分与入选（代码决定，不由模型宣布）
# ----------------------------------------------------------------------

def score_candidate(candidate: Dict[str, Any], plan: ResearchPlan) -> Dict[str, Any]:
    """给候选竞品打分并说明理由（可解释，不黑箱）。

    维度（全部结构化，与行业无关）：
    - 站点类型：像不像**产品自己的官网**（媒体/应用商店/开发者平台/收录目录/
      文章页都拿不到这一分——实测 2026-10-28，媒体站、应用商店、开发者博客
      因为"有独立域名"而拿到"有独立站点"的分，混进入选名单）
    - 数据完整度：已抽到的维度数
    - 与主体相似度：名称/描述与主体的词面重合（代理指标，不是语义判断）
    - 惩罚：命中 must_exclude
    """
    name = str(candidate.get("name") or "").strip()
    url = str(candidate.get("url") or "").strip()
    dimensions = int(candidate.get("dimension_count") or 0)
    description = str(candidate.get("description") or "")

    reasons: List[str] = []
    score = 0.0

    site_type = candidate.get("site_type")
    if not site_type:
        site_type = classify_site(url, name)
    if site_type == SITE_PRODUCT:
        score += 0.5
        reasons.append("像产品自己的官网")
    else:
        reasons.append(f"站点类型是{site_type_label(site_type)}，不是产品官网")

    score += min(0.2, dimensions * 0.06)
    if dimensions:
        reasons.append(f"已抽到 {dimensions} 个维度")

    entity_tokens = set(re.findall(r"[一-鿿]{2,}", plan.entity_name))
    desc_tokens = set(re.findall(r"[一-鿿]{2,}", description + name))
    overlap = len(entity_tokens & desc_tokens)
    if entity_tokens:
        score += min(0.25, overlap * 0.12)
        if overlap:
            reasons.append(f"与主体词面重合 {overlap} 项")

    excluded = [t for t in (plan.criteria.get("must_exclude") or []) if t and t in (name + description)]
    if excluded:
        score -= 0.5
        reasons.append("命中排除项: " + "、".join(excluded))

    return {
        "name": name,
        "url": url,
        "score": round(max(0.0, score), 4),
        "dimensions": dimensions,
        "site_type": site_type,
        "reasons": reasons,
    }


def admit_competitors(
    candidates: Sequence[Dict[str, Any]],
    plan: ResearchPlan,
) -> Dict[str, Any]:
    """按计划约束选出最终竞品集合（代码裁决，模型无权直接指定）。

    裁决依据只能是**发现阶段拿得到的信息**：有没有自己的站点、是否命中排除项、
    词面上是否与主体相关、是否被用户点名。
    「至少 2 个维度有数据」是**抓完之后**才能知道的规则，不能当发现阶段的门槛——
    拿它当门槛的结果是"维度数恒为 0 → 一家都选不出来"（实测）。
    抓完之后的完整性由矩阵层把关（content_complete 要求有效证据）。
    """
    scored = [score_candidate(c, plan) for c in candidates]
    scored.sort(key=lambda item: (-item["score"], _path_depth(item["url"]), item["name"]))
    must_include = [t for t in (plan.criteria.get("must_include") or []) if t]
    # 准入规则按**可执行形态**判（不是另起一套打分口径）：
    # 计划里写的那句口径，代码判的就是它；判不了的条款记在规则里。
    rule = parse_admission_rule(plan.criteria.get("admission_rule"), plan.criteria)
    gate_failed: List[Dict[str, Any]] = []
    for item in scored:
        passed, reasons = evaluate_discovery(item, rule)
        item["gate_passed"] = passed
        item["gate_reasons"] = reasons
        if not passed:
            gate_failed.append(item)
    selected: List[Dict[str, Any]] = []
    # 同一站点只算一家：character.ai/ 与 character.ai/about 是同一个竞品，
    # 算两家会白占一个名额（实测：8 个入选里有 2 个是同一站点的两个页面）。
    selected_hosts = set()
    for item in scored:
        if len(selected) >= plan.max_count:
            break
        if not item.get("gate_passed", True):
            continue
        host = _host_key(item["url"])
        if host and host in selected_hosts:
            continue
        if any(token and token in item["name"] for token in must_include):
            selected.append(item)
            if host:
                selected_hosts.add(host)
    for item in scored:
        if len(selected) >= plan.max_count:
            break
        if item in selected:
            continue
        if not item.get("gate_passed", True):
            continue
        host = _host_key(item["url"])
        if host and host in selected_hosts:
            continue
        if item["score"] < MIN_ADMISSION_SCORE:
            continue
        selected.append(item)
        if host:
            selected_hosts.add(host)
    return {
        "selected": selected[:plan.max_count],
        "scored": scored,
        # 被规则挡下来的候选（连同理由）——"为什么没选它"要能回答
        "gate_failed": gate_failed,
        "admission_rule": rule.to_dict(),
        "min_count": plan.min_count,
        "max_count": plan.max_count,
        "met_minimum": len(selected) >= plan.min_count,
        "pool_size": len(candidates),
    }


def render_plan_markdown(
    plan_data: Optional[Dict[str, Any]] = None,
    discovery: Optional[Dict[str, Any]] = None,
    gate: Optional[Dict[str, Any]] = None,
) -> str:
    """把研究计划 + 入选轨迹渲染成 markdown（导出用）。

    与前端「调研口径」tab 展示的是同一份数据、同一套口径：
    导出件必须能独立阅读，也要能跟界面上一一对上。
    """
    plan_data = plan_data or {}
    entity = plan_data.get("entity") or {}
    criteria = plan_data.get("competitor_criteria") or plan_data.get("criteria") or {}
    rule = plan_data.get("admission_rule") or (gate or {}).get("rule") or {}
    lines: List[str] = ["# 调研口径", ""]

    name = str(entity.get("name") or "").strip()
    if name:
        lines.append(f"## 研究主体：{name}")
        what = str(entity.get("what_it_is") or "").strip()
        if what:
            lines.append(f"- 释义：{what}")
        etype = str(entity.get("entity_type") or "").strip()
        if etype:
            lines.append(f"- 实体类型：{etype}")
        confidence = entity.get("resolved_confidence")
        if isinstance(confidence, (int, float)):
            lines.append(f"- 消歧置信度：{confidence:.2f}")
        domains = entity.get("official_domains") or []
        if domains:
            lines.append("- 主体官网域名：" + "、".join(str(d) for d in domains))
        candidates = [str(c) for c in (entity.get("disambiguation_candidates") or []) if str(c).strip()]
        if candidates:
            lines.append("- 备选：" + "、".join(candidates[:5]))
    source = str(plan_data.get("source") or "").strip()
    if source and source != "model":
        lines.append(f"- 口径来源：{source}（非模型生成）")
    lines.append("")

    lines.append("## 竞品口径")
    layers = criteria.get("layers") or []
    lines.append(f"- 层级：{'、'.join(str(l) for l in layers) or '未指定'}")
    lines.append(
        f"- 数量：{criteria.get('min_count')}-{criteria.get('max_count')} 家"
        f"（候选池 {criteria.get('candidate_pool') or '未指定'}）"
    )
    lines.append(f"- 入选标准（原文）：{criteria.get('admission_rule') or '默认'}")
    executable = [str(c) for c in (rule.get("executable") or []) if str(c).strip()]
    if executable:
        lines.append("- **实际执行的入选条件**：")
        lines.extend(f"  - {c}" for c in executable)
    not_executable = [str(c) for c in (rule.get("not_executable") or []) if str(c).strip()]
    if not_executable:
        lines.append("- **未能执行的条件**（代码不据此裁决）：")
        lines.extend(f"  - {c}" for c in not_executable)
    must_include = criteria.get("must_include") or []
    must_exclude = criteria.get("must_exclude") or []
    if must_include:
        lines.append("- 必须包含：" + "、".join(str(t) for t in must_include))
    if must_exclude:
        lines.append("- 必须排除：" + "、".join(str(t) for t in must_exclude))
    lines.append("")

    angles = plan_data.get("angles") or []
    if angles:
        lines.append("## 重点角度（权重由代码按优先级计算）")
        for angle in angles:
            label = angle.get("label") or angle.get("angle")
            weight = float(angle.get("weight") or 0)
            reason = str(angle.get("reason") or "").strip()
            lines.append(
                f"- {label} {weight:.0%}（{angle.get('priority') or 'secondary'}）"
                + (f"：{reason}" if reason else "")
            )
        lines.append("")

    discovery = discovery or {}
    selected = discovery.get("selected") or []
    candidates = discovery.get("candidates") or []
    if candidates or selected:
        lines.append("## 候选池与入选")
        lines.append(f"- 候选 {len(candidates)} 家，入选 {len(selected)} 家")
        scored = discovery.get("scored") or []
        selected_urls = {str(s.get("url") or "") for s in selected}
        for item in scored or candidates:
            url = str(item.get("url") or "")
            mark = "✓" if url in selected_urls else "✗"
            site_type = str(item.get("site_type") or "")
            score = item.get("score")
            reasons = "；".join(str(r) for r in (item.get("reasons") or []) if str(r).strip())
            detail = f"（得分 {score}）" if isinstance(score, (int, float)) else ""
            lines.append(
                f"- {mark} {item.get('name') or url}"
                + (f" · {site_type}" if site_type else "")
                + detail
                + (f" · {reasons}" if reasons else "")
            )
            lines.append(f"  - {url}")
        notes = [str(n) for n in (discovery.get("notes") or []) if str(n).strip()]
        if notes:
            lines.append("")
            lines.append("### 过程记录")
            lines.extend(f"- {n}" for n in notes)
        lines.append("")

    rejected = (gate or {}).get("rejected") or []
    if rejected:
        lines.append("## 未采信（数据不足，已剔除）")
        for item in rejected:
            reasons = "；".join(str(r) for r in (item.get("reasons") or []) if str(r).strip())
            lines.append(
                f"- {item.get('name') or item.get('url')}"
                + (f"（抽到 {item.get('dimension_count')} 个维度）" if item.get("dimension_count") is not None else "")
                + (f"：{reasons}" if reasons else "")
            )
        lines.append("")

    questions = plan_data.get("open_questions") or []
    if questions:
        lines.append("## 开放问题")
        for item in questions:
            question = str(item.get("question") or "").strip()
            default = str(item.get("default") or "").strip()
            lines.append(f"- {question}" + (f"（默认：{default}）" if default else ""))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _host_key(url: str) -> str:
    """去 www. 的域名（同一站点的判定键）。"""
    host = re.sub(r"^https?://", "", (url or "").strip()).split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _path_depth(url: str) -> int:
    """路径段数——首页最浅（0 段或 1 段）。"""
    path = re.sub(r"^https?://[^/]+", "", (url or "").strip())
    return len([s for s in path.split("/") if s])
