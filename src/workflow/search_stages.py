"""搜索四段式：实体锚定 → 竞品发现 → 逐家验证 → 反证。

为什么要分段
------------
今天只有一段：把整句任务描述丢给引擎，取前 N 个 URL 就当竞品去抓。
后果实测过：任务句式下最该抓的"官方介绍页"和"竞品盘点页"会被相关性过滤
丢掉（query 里的"找一下/全部/的/信息"稀释覆盖率），而搜索引擎首页里的
媒体/百科/社区页会被当成竞品。

分段之后每段有**自己的目标和验收标准**：

1. **实体锚定**：目标是弄清"主体是什么"，不是找竞品。结果用于消歧与
   官方源确认，不进竞品池。
2. **竞品发现**：目标是产出**候选池**（可能 8–12 家），不是直接抓。
   候选池随后按计划的标准打分入选——**入选由代码裁决**，模型无权宣布
   "这就是竞品"。
3. **逐家验证**：对已入选的每家，按角度找它自己的相关页面（站内选页
   优先级 + 站外 site: 查询）。
4. **反证**：专门找缺点与评价。这一段最容易被漏掉，于是报告里
   limitations/reviews 常年空着——而那恰恰是采购与评审最想看的。

失败即降级：任何一段没有结果都不阻断，交给下一段或退回旧行为。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from src.workflow.research_plan import ResearchPlan, admit_competitors
from src.workflow.site_types import (  # noqa: F401  (转出以兼容既有引用)
    SITE_APP_STORE,
    SITE_DIRECTORY,
    SITE_MEDIA,
    SITE_PRODUCT,
    SITE_UGC,
    SITE_UNKNOWN,
    classify_site,
    host_of,
    looks_like_aggregator,
    site_type_label,
)

# ---- 站点类型分类 ----------------------------------------------------
# 实现见 src/workflow/site_types.py（独立模块，避免 research_plan ↔ search_stages
# 循环导入）。上面已转出 classify_site / looks_like_aggregator 等符号。

# 逐家验证/反证阶段，每家每阶段最多发几条查询（词数会乘上竞品数与阶段数）
MAX_QUERIES_PER_STAGE = 2

# 模型提名的竞品最多核实几个（每个要发 2 条查询，词数会乘上去）
MAX_MODEL_CANDIDATES = 8

# 模型有时会写**占位符**而不是真词（实测："{竞品名} 官网"）。
# 直接拿去搜索就是在搜字面量——必须先填成真的竞品名。
_PLACEHOLDER_PATTERNS = (
    "{竞品名}", "{竞品}", "{competitor}", "{competitor_name}", "{{竞品名}}",
    "<竞品名>", "【竞品名】", "[竞品名]", "{name}", "<name>",
    # 裸写的占位符（实测模型给过 "竞品名 官网"，不带括号）
    "竞品名", "竞品名称",
)


def _expand_placeholder(query: str, competitor: str) -> str:
    """把查询词里的竞品名占位符换成真名。"""
    text = query or ""
    for pattern in _PLACEHOLDER_PATTERNS:
        text = text.replace(pattern, competitor)
    return text.strip()


def _strip_www(host: str) -> str:
    """去掉 www. 前缀——官网域名常写成 www.dumate.cn，模型给的可能是 dumate.cn。"""
    host = (host or "").split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _is_subject_itself(candidate_name: str, entity_name: str) -> bool:
    """候选是不是主体自己。

    实测：主体"百度搭子"在候选里以"百度搭子 DuMate"（同一产品的另一个叫法）
    出现，精确相等判不出来 → 主体被当成自己的竞品进了入选名单。
    规则：候选名**以主体名开头**（允许后接空格后的补充说明）即为自身；
    不做子串包含（"美菜" vs "美菜网"是同一家公司但**是**竞品口径上的另一个产品，
    按包含判会把真竞品排掉）。
    """
    name = (candidate_name or "").strip()
    subject = (entity_name or "").strip()
    if not name or not subject:
        return False
    if name == subject:
        return True
    return name.startswith(subject + " ") or name.startswith(subject + "-")


def clean_candidate_name(title: str, url: str) -> str:
    """从搜索结果标题里粗提候选名（去站点后缀、去噪声）。"""
    name = (title or "").strip()
    name = re.split(r"\s*[-|｜–—_]\s*", name)[0].strip()
    name = re.sub(r"\s*[-—_]?\s*(官网|首页|官方网站|中文站)$", "", name).strip()
    if not name:
        host = host_of(url)
        name = host.split(".")[0] if host else url
    return name[:40]


@dataclass
class Candidate:
    name: str
    url: str
    description: str = ""
    dimension_count: int = 0
    source_stage: str = ""
    is_aggregator: bool = False
    site_type: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "description": self.description[:200],
            "dimension_count": self.dimension_count,
            "source_stage": self.source_stage,
            "is_aggregator": self.is_aggregator,
            "site_type": self.site_type,
        }


@dataclass
class DiscoveryResult:
    entity_pages: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    selected: List[Dict[str, Any]] = field(default_factory=list)
    scored: List[Dict[str, Any]] = field(default_factory=list)
    queries_run: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_pages": self.entity_pages,
            "candidates": [c.to_dict() for c in self.candidates],
            "selected": self.selected,
            "scored": self.scored,
            "queries_run": self.queries_run,
            "notes": self.notes,
        }


class SearchStageRunner:
    """按计划执行搜索阶段。search_tool 是既有 SearchTool（可注入替身）。"""

    def __init__(self, search_tool: Any, num_results: int = 8):
        self.search_tool = search_tool
        self.num_results = num_results
        # 主体锚定的结果缓存：竞品发现阶段要据此排除"主体自己的官方页"
        self._entity_pages: List[Dict[str, Any]] = []
        # 主体自己官网的域名：按"实体锚定阶段排名最高的官网"判定。
        # 实测（2026-10-28）：主体"百度搭子"的产品官网叫 dumate.cn（百度通用桌面
        # AI 智能体），名字跟主体名**完全不一样**——按名字排不掉，只能靠"锚定阶段
        # 排第一的产品官网就是主体自己"这个结构事实来排。
        self._entity_official_host: str = ""

    # -- 单次查询 --------------------------------------------------------
    def _search(self, query: str, stage: str) -> List[Dict[str, Any]]:
        try:
            results = self.search_tool.search(query, num_results=self.num_results)
        except Exception:  # noqa: BLE001 —— 搜索失败不该拖垮链路
            return []
        clean: List[Dict[str, Any]] = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            clean.append({
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "content": str(item.get("content") or item.get("snippet") or "").strip(),
            })
        return clean

    def _run_stage(self, plan: ResearchPlan, stage: str) -> List[Dict[str, Any]]:
        queries = plan.queries_for(stage)
        collected: List[Dict[str, Any]] = []
        seen_urls = set()
        for query in queries:
            results = self._search(query, stage)
            for item in results:
                # 按 URL 去重：同一条查询会返回同一批页面，多条查询更是重叠。
                # 不去重的话"取前 5 条"会变成"同一个 URL 占 4 个名额"，
                # 实体锚定阶段就会只覆盖到 1–2 个站点（实测）。
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                collected.append(item)
        return collected

    # -- 阶段 1：实体锚定 -------------------------------------------------
    def run_entity_anchor(self, plan: ResearchPlan) -> List[Dict[str, Any]]:
        """目标是弄清主体是什么，不是找竞品。

        排序：非媒体页优先 → 标题里带主体名的优先 → 标题短优先。
        最后一条是"标题短优先"而不是"长优先"：短标题通常是产品页
        （"猫箱"），长标题通常是文章页（"2024年AI社交产品盘点:…"）。
        """
        pages = self._run_stage(plan, "entity_anchor")
        entity_tokens = [t for t in re.split(r"[\s\-_｜|]+", plan.entity_name or "") if t]

        def _key(item: Dict[str, Any]) -> Tuple:
            title = item["title"]
            mentions = 0 if not entity_tokens else int(
                any(tok in title for tok in entity_tokens)
            )
            return (looks_like_aggregator(item["url"]), -mentions, len(title))

        pages.sort(key=_key)
        self._entity_pages = pages[:5]
        # 排名最高的**产品官网**就是主体自己的官网（锚定阶段问的就是"主体是什么/
        # 官网"，媒体页已排在前面的除外）
        for page in self._entity_pages:
            if classify_site(page["url"], page["title"]) == SITE_PRODUCT:
                self._entity_official_host = host_of(page["url"]).split(":")[0]
                break
        return self._entity_pages

    # -- 阶段 2：竞品发现 + 入选 ------------------------------------------
    def run_competitor_discovery(self, plan: ResearchPlan) -> DiscoveryResult:
        result = DiscoveryResult()
        result.entity_pages = list(self._entity_pages)
        results = self._run_stage(plan, "competitor_discovery")
        # 模型提名的竞品：它们只是**候选**，要自己去搜官网、判是不是产品官网。
        # 为什么需要这一步（实测 2026-10-28）：搜索框搜「猫箱 竞品」回来的
        # 全是榜单文章，判完一个不剩；而模型其实知道同赛道是谁
        # （星野/筑梦岛/Talkie/Character.AI）。**模型提名，代码裁决。**
        for name in (plan.competitor_candidates or [])[:MAX_MODEL_CANDIDATES]:
            for query in (f"{name} 官网", f"{name} 下载"):
                for item in self._search(query, "competitor_discovery"):
                    results.append(item)
        seen = set()
        by_name: Dict[str, Candidate] = {}
        model_names = [n.strip() for n in (plan.competitor_candidates or []) if n.strip()]
        for item in results:
            url = item["url"]
            if url in seen:
                continue
            seen.add(url)
            title = item["title"]
            # 候选名优先用**模型提名**的名字：搜索结果的标题常是文章标题
            # （"Lovie 常見問題：AI 角色陪伴、AI 聊天與角色扮演"），
            # 拿它当竞品名会让后续的验证/反证查询全跑偏。
            name = clean_candidate_name(title, url)
            for proposed in model_names:
                if proposed and (proposed in title or title.startswith(proposed)):
                    name = proposed
                    break
            candidate = Candidate(
                name=name,
                url=url,
                description=item["content"],
                source_stage="competitor_discovery",
                # 站点类型要连**标题**一起判：中文搜索结果里文章页的决定性
                # 特征在标题里（盘点/对比/哪个好），域名未必看得出来。
                site_type=classify_site(url, title),
                is_aggregator=classify_site(url, title) != SITE_PRODUCT,
            )
            result.candidates.append(candidate)
            # 同名去重：同一个产品常在多个页面里出现（官网 + 媒体盘点页）。
            # 保留**官网**那份——媒体页已被标记为 aggregator，先到先得的
            # 做法会把官网挤掉。
            existing = by_name.get(candidate.name)
            if existing is None or (existing.is_aggregator and not candidate.is_aggregator):
                by_name[candidate.name] = candidate
        # 主体自己的官方页不是竞品。但"官方页"要判准：竞品官网的首页常提到主体
        # （"猫箱 - 与百度搭子对比"），若凡是出现在锚定结果里的 URL 都算主体页，
        # 就会把真竞品一起排掉（实测）。判准 = 标题里带主体名**且**页面就是主体页。
        entity_name = (plan.entity_name or "").strip()
        entity_urls = {p["url"] for p in (result.entity_pages or [])}
        self_page_urls = {
            p["url"] for p in (result.entity_pages or [])
            if entity_name and entity_name in str(p.get("title") or "")
        }
        before = len(result.candidates)
        # 主体自己的官网域名：模型给的（entity.official_domains）+ 锚定阶段
        # 排第一的产品官网。主体的产品名常与用户说法不同（"百度搭子"=dumate.cn），
        # 只按名字排不掉，必须按域名排。
        subject_hosts = set(
            [_strip_www(self._entity_official_host)] if self._entity_official_host else []
        )
        subject_hosts.update(
            _strip_www(host_of(f"https://{d}")) for d in (plan.official_domains or [])
        )
        subject_hosts.discard("")
        result.candidates = [
            c for c in result.candidates
            if c.url not in self_page_urls
            and not _is_subject_itself(c.name, entity_name)
            # 主体自己官网域名下的页面：主体不跟自己的竞品并列
            and _strip_www(host_of(c.url)) not in subject_hosts
        ]
        dropped_self = before - len(result.candidates)
        if dropped_self:
            result.notes.append(f"排除主体自身的 {dropped_self} 个页面（主体不是自己的竞品）")
        # 媒体/百科/社区/应用商店/开发者平台/收录目录/文章页都不是竞品官网：
        # 剔掉（但保留在候选里以备人工查看）
        official = [c for c in result.candidates if not c.is_aggregator]
        if official:
            dropped = [c for c in result.candidates if c.is_aggregator]
            by_type: Dict[str, int] = {}
            for c in dropped:
                by_type[c.site_type or SITE_UNKNOWN] = by_type.get(c.site_type or SITE_UNKNOWN, 0) + 1
            type_desc = "、".join(
                f"{site_type_label(t)} {n} 个" for t, n in sorted(by_type.items())
            )
            result.notes.append(
                f"候选池剔除 {len(dropped)} 个非官网页（{type_desc}）："
                + "、".join(f"{c.name}({c.url})" for c in dropped[:5])
            )
            pool = official
        else:
            # 一个官网都没剩：按口径**不拿媒体页充数**。宁可空手回去说
            # "没找到竞品官网"，也不把知乎/百科写成竞品。
            result.notes.append(
                "候选池里没有竞品官网（只有媒体/社区页）——按口径不入选，"
                "本轮可能找不到竞品"
            )
            pool = []
        admission = admit_competitors([c.to_dict() for c in pool], plan)
        result.selected = admission["selected"]
        result.scored = admission["scored"]
        if not admission["met_minimum"]:
            result.notes.append(
                f"入选 {len(admission['selected'])} 家，低于计划下限 {admission['min_count']} 家"
            )
        return result

    # -- 阶段 3/4：逐家验证与反证 ----------------------------------------
    def site_scoped_queries(self, plan: ResearchPlan, competitor: str, stage: str) -> List[str]:
        """给某家竞品生成站外查询词（site: 限定 + 阶段意图）。"""
        base = plan.queries_for(stage)
        intent = {
            "per_competitor_verify": ["定价", "功能", "方案"],
            "counter_evidence": ["缺点", "评价", "投诉", "问题"],
        }.get(stage, [])
        queries = []
        for query in base:
            expanded = _expand_placeholder(query, competitor)
            # 模型的查询词是**模板**：要么带占位符（"{竞品名} 官网"），
            # 要么是通用词（"官网"）。两种都得把真竞品名放进去，
            # 否则搜出来的是"竞品名 官网"这种字面量。
            if competitor and competitor not in expanded:
                expanded = f"{expanded} {competitor}"
            queries.append(expanded)
        for word in intent:
            queries.append(f"{competitor} {word}")
        # 去重且保持顺序：模型的查询词里可能已经带了这家竞品名
        seen = set()
        unique = []
        for query in queries:
            query = query.strip()
            if not query or query in seen:
                continue
            seen.add(query)
            unique.append(query)
        return unique

    def run_verification(self, plan: ResearchPlan, competitors: Sequence[str]) -> List[Dict[str, Any]]:
        """对已入选竞品做站外补充检索（逐家验证 + 反证）。

        每家每阶段**只发有限几条查询**：竞品数 × 阶段 × 词数会放大成几十次
        搜索（5 家 × 2 阶段 × 4 词 = 40 次），延迟与配额都扛不住。
        先发最能定位的词，够用就停。
        """
        pages: List[Dict[str, Any]] = []
        seen = set()
        for competitor in competitors:
            for stage in ("per_competitor_verify", "counter_evidence"):
                queries = self.site_scoped_queries(plan, competitor, stage)
                for query in queries[:MAX_QUERIES_PER_STAGE]:
                    for item in self._search(query, stage):
                        if item["url"] in seen:
                            continue
                        seen.add(item["url"])
                        item["stage"] = stage
                        item["competitor"] = competitor
                        pages.append(item)
        return pages
