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
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

from src.workflow.research_plan import ResearchPlan, admit_competitors

# 候选页里常见的"不是竞品官网"的域名特征（媒体/百科/社区/聚合站）。
# 这不是行业知识，是**站点类型**——竞品官网 vs 内容站，与做什么无关。
_AGGREGATOR_HOST_HINTS = (
    "zhihu.", "baijiahao.", "sohu.", "163.com", "qq.com", "weibo.",
    "wikipedia.", "baike.", "douban.", "xiaohongshu.", "bilibili.",
    "toutiao.", "cnki.", "csdn.", "jianshu.", "medium.com", "youtube.",
    "facebook.com", "twitter.com", "x.com", "linkedin.com", "reddit.com",
)

_MEDIA_PATH_HINTS = ("/article", "/news", "/blog", "/post", "/topic", "/wiki", "/comment")

# 逐家验证/反证阶段，每家每阶段最多发几条查询（词数会乘上竞品数与阶段数）
MAX_QUERIES_PER_STAGE = 2


def _host(url: str) -> str:
    try:
        return urlparse(url or "").netloc.lower()
    except ValueError:
        return ""


def looks_like_aggregator(url: str) -> bool:
    """看起来是媒体/百科/社区/聚合站——大概率不是竞品官网。"""
    host = _host(url)
    if not host:
        return True
    if any(hint in host for hint in _AGGREGATOR_HOST_HINTS):
        return True
    path = (urlparse(url).path or "").lower()
    return any(hint in path for hint in _MEDIA_PATH_HINTS)


def clean_candidate_name(title: str, url: str) -> str:
    """从搜索结果标题里粗提候选名（去站点后缀、去噪声）。"""
    name = (title or "").strip()
    name = re.split(r"\s*[-|｜–—_]\s*", name)[0].strip()
    name = re.sub(r"\s*[-—_]?\s*(官网|首页|官方网站|中文站)$", "", name).strip()
    if not name:
        host = _host(url)
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "description": self.description[:200],
            "dimension_count": self.dimension_count,
            "source_stage": self.source_stage,
            "is_aggregator": self.is_aggregator,
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
            collected.extend(results)
            for item in results:
                seen_urls.add(item["url"])
        return collected

    # -- 阶段 1：实体锚定 -------------------------------------------------
    def run_entity_anchor(self, plan: ResearchPlan) -> List[Dict[str, Any]]:
        """目标是弄清主体是什么，不是找竞品。"""
        pages = self._run_stage(plan, "entity_anchor")
        pages.sort(key=lambda item: (looks_like_aggregator(item["url"]), len(item["title"])))
        return pages[:5]

    # -- 阶段 2：竞品发现 + 入选 ------------------------------------------
    def run_competitor_discovery(self, plan: ResearchPlan) -> DiscoveryResult:
        result = DiscoveryResult()
        results = self._run_stage(plan, "competitor_discovery")
        seen = set()
        for item in results:
            url = item["url"]
            if url in seen:
                continue
            seen.add(url)
            result.candidates.append(Candidate(
                name=clean_candidate_name(item["title"], url),
                url=url,
                description=item["content"],
                source_stage="competitor_discovery",
                is_aggregator=looks_like_aggregator(url),
            ))
        # 媒体/百科/社区页不是竞品官网：剔掉（但保留在候选里以备人工查看）
        official = [c for c in result.candidates if not c.is_aggregator]
        if official:
            dropped = [c for c in result.candidates if c.is_aggregator]
            result.notes.append(
                f"候选池剔除 {len(dropped)} 个媒体/社区页："
                + "、".join(f"{c.name}({c.url})" for c in dropped[:5])
            )
        admission = admit_competitors(
            [c.to_dict() for c in (official or result.candidates)], plan
        )
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
            queries.append(f"{query} {competitor}")
        for word in intent:
            queries.append(f"{competitor} {word}")
        return queries

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
