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
from src.tools.search_tool import SearchBackendError
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
    normalized_brand,
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


def _title_is_exactish(title: str, name: str) -> bool:
    """标题是否就是这个名字（去掉常见的后缀分隔）。

    实测："星野"（AI 应用）与"星野集团"（一家酒店公司）同名，靠这个判据
    留下的是真产品那个。

    比较**忽略大小写**：同一个产品的官网与移动站一个写 "Tofai" 一个写 "tofai"
    （实测第六轮：tofaipro.cn 与 m.tofai.com.cn 各占一个名额）。
    """
    text = (title or "").strip()
    target = (name or "").strip()
    if not text or not target:
        return False
    for sep in (" - ", " | ", "｜", " — ", "_", "－"):
        text = text.split(sep)[0].strip()
    return text.casefold() == target.casefold()


def _strip_www(host: str) -> str:
    """去掉 www. 前缀——官网域名常写成 www.dumate.cn，模型给的可能是 dumate.cn。"""
    host = (host or "").split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


# 候选名里的**修饰词**与规范化在 site_types.normalized_brand
# （research_plan 也要用它给"模型提名的竞品"加权重，放一起才没有循环导入）。

def _normalized_brand(name: str) -> str:
    """候选名的品牌键（去修饰词 + 去标点）——实现在 site_types，这里转出。"""
    return normalized_brand(name)


def _brand_in_host(brand: str, host: str) -> bool:
    """品牌键是否出现在域名里（同一产品的镜像站/子域）。

    实测第八轮："Kindroid"（kindroid.ai）与 "Kindred Stitch"（kindroid-ai.net）
    名字对不上，但域名里带着品牌 → 是同一家的两个站。

    门槛：品牌键至少 5 个字符——太短的（"ai"、"app"）会在无关域名里乱命中。
    域名先去掉点/连字符再比："character.ai" → "characterai"，
    于是品牌 "characterai" 命中官网、不命中汉化站 "characterapp.cn"。
    """
    brand = (brand or "").strip().casefold()
    host = (host or "").strip().casefold()
    if len(brand) < 5 or not brand.isascii():
        return False
    flat = "".join(ch for ch in host if ch.isalnum())
    return brand in flat


def _candidate_rank(candidate: Any) -> Tuple[int, int, int]:
    """候选的"更像竞品官网"排序（越大越好）。

    1. 非聚合页优先（官网 > 媒体/商店页）；
    2. 域名里带自己的品牌优先（kindroid.ai > kindroid-ai.net）；
    3. 域名更短优先（少一层子域的多半是官网首页）。
    """
    host = _strip_www(host_of(candidate.url))
    brand = _normalized_brand(candidate.name)
    return (
        0 if candidate.is_aggregator else 1,
        1 if _brand_in_host(brand, host) else 0,
        -len(host),
    )


def _merge_brand_hosts(by_name: Dict[str, Any]) -> Dict[str, Any]:
    """第二轮合并：**域名里带同一品牌**的候选算同一家。

    实测第八轮："Kindroid"（kindroid.ai）与 "Kindred Stitch"（kindroid-ai.net）
    名字对不上，按名字合并漏掉了——同一家的两个站白占两个名额。
    """
    kept: List[Tuple[str, Any]] = []
    for key, candidate in by_name.items():
        merged_key = None
        for other_key, other in kept:
            if _brand_in_host(_normalized_brand(candidate.name), host_of(other.url)) \
                    or _brand_in_host(_normalized_brand(other.name), host_of(candidate.url)):
                merged_key = other_key
                break
        if merged_key is None:
            kept.append((key, candidate))
            continue
        # 同一家的两个站：留更像官网的那个
        if _candidate_rank(candidate) > _candidate_rank(
                next(c for k, c in kept if k == merged_key)):
            kept = [(k, candidate if k == merged_key else c) for k, c in kept]
    return dict(kept)


def _canonical_product_url(url: str) -> str:
    """把产品官网的子页归一到**首页**（根路径）。

    实测（2026-10-29 召回测量）：搜"星野"回来的常常是
    `xingyeai.com/create/login` 这类子页，而"产品官网首页是根路径或一层"
    的判定会把子页当成非官网 → **产品明明找到了却被丢掉**（实测丢失了
    星野、筑梦岛等好几个金标准产品）。归一后 URL 才是那条判定能认的形态。
    """
    raw = (url or "").strip()
    if not raw or "//" not in raw:
        return url
    try:
        parsed = urlparse(raw)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if not host:
        return url
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}/"


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
    # 本轮搜索后端是否失败过（引擎集体超时/被限流）。
    # 它与"这个查询真的没有结果"必须分开：前者是**本轮数据无效**，
    # 后者才是"没找到竞品"。实测（2026-10-29）两者混为一谈，
    # 整组结论作废还看不出原因。
    search_failed: bool = False
    search_failures: List[Dict[str, Any]] = field(default_factory=list)
    # C3 目录页通道读过的页面（测量时要拿它剔除"通道自证条目"）
    channel_pages: List[str] = field(default_factory=list)
    # 通道贡献的候选（按 URL 记）
    channel_candidates: List[str] = field(default_factory=list)
    # C4 应用商店通道：读过的分类/榜单页 + 贡献的候选（同 C3 的观测口径 ✓）
    store_pages: List[str] = field(default_factory=list)
    store_candidates: List[str] = field(default_factory=list)
    # 通道自己的 site: 查询失败记录（**不**算整轮无效的依据 ✗）
    channel_failures: List[Dict[str, Any]] = field(default_factory=list)
    # 名额账（谁占了名额、依据、谁被挤掉）
    slot_ledger: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_pages": self.entity_pages,
            "candidates": [c.to_dict() for c in self.candidates],
            "selected": self.selected,
            "scored": self.scored,
            "queries_run": self.queries_run,
            "notes": self.notes,
            "search_failed": self.search_failed,
            "search_failures": self.search_failures,
            "channel_pages": list(self.channel_pages),
            "channel_candidates": list(self.channel_candidates),
            "store_pages": list(self.store_pages),
            "store_candidates": list(self.store_candidates),
            "channel_failures": list(self.channel_failures),
            "slot_ledger": dict(self.slot_ledger),
        }


class SearchStageRunner:
    """按计划执行搜索阶段。search_tool 是既有 SearchTool（可注入替身）。"""

    def __init__(self, search_tool: Any, num_results: int = 8,
                 enable_directory_channel: bool = True,
                 enable_store_channel: bool = True):
        self.search_tool = search_tool
        self.num_results = num_results
        # C3 目录页通道（默认开）：模型不知道、搜索也搜不到的长尾产品，
        # 大量只在目录站的**分类页**上（一页几十个，服务端渲染 ✓）。
        # 通道读过的页要记进 result.channel_pages —— 测量时要把
        # "来源正好是这些页"的金标准条目从分母里剔掉（那叫读答案 ✓）。
        self.enable_directory_channel = enable_directory_channel
        # C4 应用商店通道（默认开）：store_only 来源占全集 32% ✗，
        # 商店 Web 版的深页在常规查询里不出现 ✓。
        self.enable_store_channel = enable_store_channel
        # 主体锚定的结果缓存：竞品发现阶段要据此排除"主体自己的官方页"
        self._entity_pages: List[Dict[str, Any]] = []
        # 主体自己官网的域名：按"实体锚定阶段排名最高的官网"判定。
        # 实测（2026-10-28）：主体"百度搭子"的产品官网叫 dumate.cn（百度通用桌面
        # AI 智能体），名字跟主体名**完全不一样**——按名字排不掉，只能靠"锚定阶段
        # 排第一的产品官网就是主体自己"这个结构事实来排。
        self._entity_official_host: str = ""
        # 搜索后端的失败记录（引擎集体超时/限流）——见 DiscoveryResult.search_failed
        self.search_failures: List[Dict[str, Any]] = []
        # **辅助通道**的失败单独记：通道是补充渠道，它的 site: 查询失败
        # 不代表"这轮候选池不完整"（实测 2026-11-01：www.ai-bot.cn 的
        # site: 查询稳定失败，而主搜索全部成功 ⇒ 不该作废整轮 ✗）。
        self.channel_failures: List[Dict[str, Any]] = []

    # -- 单次查询 --------------------------------------------------------
    def _search(self, query: str, stage: str,
                count_failure: bool = True) -> List[Dict[str, Any]]:
        try:
            results = self.search_tool.search(query, num_results=self.num_results)
        except SearchBackendError as exc:
            # 后端整轮没给出结果（引擎超时/限流）：这是**本轮数据无效**，
            # 不是"这个查询没找到"。记下来，别让它伪装成 0 家竞品。
            #
            # 但**辅助通道**的搜索失败不该作废整轮 ✗✗：
            # 实测（2026-11-01 第十轮）C3 通道的 `site:` 查询
            # 在 www.ai-bot.cn 上稳定报"搜索引擎无响应"，
            # 而这一轮的主搜索全部成功 ⇒ 整轮被误判为无效 ✗✗。
            # 所以通道的失败单独记（channel_failures ✓），不影响主链路 ✓。
            if count_failure:
                self.search_failures.append({
                    "stage": stage, "query": query, "error": str(exc),
                })
            else:
                self.channel_failures.append({
                    "stage": stage, "query": query, "error": str(exc),
                })
            return []
        except Exception:  # noqa: BLE001 —— 其它搜索失败不该拖垮链路
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
            # 查询词：实测（2026-10-29）"<名> 下载"几乎搜不到官网——
            # "筑梦岛 下载" 0 条，"星野 下载" 只回下载站，而 "<名> 官方网站"
            # 能捞到官网首页（星野官方网站 → xingyeai.com）。下载站页面一律
            # 被站点类型判定剔掉，所以这一路查询**只有污染没有贡献**。
            for query in (f"{name} 官网", f"{name} 官方网站"):
                for item in self._search(query, "competitor_discovery"):
                    results.append(item)
        seen = set()
        by_name: Dict[str, Candidate] = {}
        title_by_name: Dict[str, str] = {}
        model_names = [n.strip() for n in (plan.competitor_candidates or []) if n.strip()]
        # -- C3 目录页通道 ------------------------------------------------
        # 在**合并同名**之前把通道条目并进来：它们和搜索结果是同形
        # （title/url/content ✓），走同一套去重、站点类型判定与闸门 ✓。
        if self.enable_directory_channel and (plan.track_keywords or []):
            try:
                from src.tools.list_pages import collect_channel_items

                channel_items, channel_notes, channel_pages = collect_channel_items(
                    lambda query: self._search(query, "competitor_discovery",
                                               count_failure=False),
                    plan.track_keywords or [],
                )
                results.extend(channel_items)
                result.channel_pages = list(channel_pages)
                result.channel_candidates = [item["url"] for item in channel_items]
                result.notes.extend(channel_notes)
                if self.channel_failures:
                    result.notes.append(
                        f"C3 通道有 {len(self.channel_failures)} 次 site: 查询失败"
                        f"（不作为本轮无效的依据 ✗——主搜索成功即数据有效）")
            except Exception as exc:  # noqa: BLE001 —— 通道失败不能拖垮整轮
                result.notes.append(f"C3 通道异常（已跳过）：{type(exc).__name__}")
        # -- C4 应用商店通道 -----------------------------------------------
        # 商店 Web 版的详情/榜单页在常规查询里根本不出现 ✓（store_only 占
        # 全集 32%，是 R1 最大的已知盲区）。条目会带**开发者官网链接**
        # （官网富化 ✓），同形并入候选池；读过的榜单页记进 store_pages
        # （测量时同 C3 一样剔"通道自证条目" ✓）。
        if self.enable_store_channel and (plan.track_keywords or []):
            try:
                from src.tools.store_pages import collect_store_items

                store_items, store_notes, store_page_list = collect_store_items(
                    lambda query: self._search(query, "competitor_discovery",
                                               count_failure=False),
                    plan.track_keywords or [],
                )
                results.extend(store_items)
                result.store_pages = list(store_page_list)
                result.store_candidates = [item["url"] for item in store_items]
                result.notes.extend(store_notes)
            except Exception as exc:  # noqa: BLE001 —— 通道失败不能拖垮整轮
                result.notes.append(f"C4 通道异常（已跳过）：{type(exc).__name__}")
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
            matched_model_name = ""
            for proposed in model_names:
                if proposed and (proposed in title or title.startswith(proposed)):
                    name = proposed
                    matched_model_name = proposed
                    break
            # 搜到的常常是产品官网的**子页**（星野 → xingyeai.com/create/login），
            # 而"产品官网首页"是根路径或一层 → 子页会被判成非官网，**产品就此丢掉**。
            # 实测（2026-10-29 召回测量）：星野/筑梦岛被找到的次数不少，
            # 但每次都以子页形态出现，于是永远进不了候选池。
            # 归一化只在这一种情况下做：页面标题**正好就是**模型提名的那个名字
            # （下载站页面的标题是"星野下载-…"，不是"星野"，所以不会被误归一）。
            if matched_model_name and _title_is_exactish(title, matched_model_name):
                url = _canonical_product_url(url)
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
            # 同名去重：同一个产品常在多个页面里出现（官网 + 媒体盘点页，
            # 或者是同产品的多个域名：replika.com / create.replika.com /
            # myreplika.com）。留**官网**那份；同名里若有一份标题与名字**完全
            # 一致**，它更像产品本身（"星野" vs 同名的酒店公司"星野集团"）。
            # 合并键**忽略大小写**：同一产品的官网与移动站常常一个写 "Tofai"
            # 一个写 "tofai"（实测第六轮：两个域名各占一个名额）。
            #
            # 合并键用**规范化品牌名**（去修饰词 + 去标点）：实测第八轮
            # "Character.AI" 与 "Character AI中文版"（汉化站）名字不同，
            # 于是同一个产品白占两个名额，把真竞品挤出去了。
            merge_key = _normalized_brand(candidate.name)
            if not merge_key:
                merge_key = candidate.name.strip().casefold()
            existing = by_name.get(merge_key)
            if existing is None:
                by_name[merge_key] = candidate
                title_by_name[merge_key] = title
            else:
                better = False
                if existing.is_aggregator and not candidate.is_aggregator:
                    better = True
                elif not existing.is_aggregator and not candidate.is_aggregator:
                    # 同一家的两个站：留更像官网的那个（域名里带自己品牌、
                    # 域名更短）。实测第八轮：汉化站（characterapp.cn）会把
                    # 真官网（character.ai）挤掉——只按"标题更贴名字"判会选错。
                    if _candidate_rank(candidate) > _candidate_rank(existing):
                        better = True
                    elif _candidate_rank(candidate) == _candidate_rank(existing):
                        if _title_is_exactish(title, candidate.name) and not _title_is_exactish(
                            title_by_name.get(merge_key, ""), candidate.name
                        ):
                            better = True
                if better:
                    by_name[merge_key] = candidate
                    title_by_name[merge_key] = title
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
        # 同名合并表要跟着一起筛：主体自己的页被排掉后，不能又从 by_name 里回来
        surviving = {c.url for c in result.candidates}
        by_name = {
            name: c for name, c in by_name.items() if c.url in surviving
        }
        # 第二轮合并：域名里带同一品牌的候选算同一家（kindroid.ai / kindroid-ai.net）
        by_name = _merge_brand_hosts(by_name)
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
            # 同名合并后再送裁决：同一个产品的多个域名/页面只算一个竞品，
            # 否则一个名额会被同一个产品占掉（实测：Replika 三个域名占了三个名额）
            pool = [c for c in by_name.values() if not c.is_aggregator]
        else:
            # 一个官网都没剩：按口径**不拿媒体页充数**。宁可空手回去说
            # "没找到竞品官网"，也不把知乎/百科写成竞品。
            # 注意区分两种空：**一条候选都没搜到**（后端抖动或查询无效）
            # 与"搜到了但全是媒体页"——措辞不能混，否则把故障说成"没竞品"。
            if not result.candidates:
                result.notes.append(
                    "本轮搜索一条候选都没搜到（候选池为空）——"
                    "这一轮没有可用数据，不能当成『没找到竞品』"
                )
            else:
                result.notes.append(
                    "候选池里没有竞品官网（只有媒体/社区页）——按口径不入选，"
                    "本轮可能找不到竞品"
                )
            pool = []
        admission = admit_competitors([c.to_dict() for c in pool], plan)
        result.selected = admission["selected"]
        result.scored = admission["scored"]
        # 名额账：每个名额给了谁、依据是什么、谁被挤掉了（纯观测 ✗）
        result.slot_ledger = admission.get("slot_ledger") or {}
        if not admission["met_minimum"]:
            result.notes.append(
                f"入选 {len(admission['selected'])} 家，低于计划下限 {admission['min_count']} 家"
            )
        # 搜索后端失败过的这一轮：**数据无效**，不能当成"没找到竞品"。
        if self.search_failures:
            result.search_failed = True
            result.search_failures = list(self.search_failures)
            stages = sorted({f["stage"] for f in self.search_failures})
            result.notes.append(
                f"⚠ 本轮有 {len(self.search_failures)} 次搜索后端失败（阶段：{'、'.join(stages)}）"
                f"——候选池不完整，本轮数据不能当作『没找到竞品』"
            )
        # 通道的失败**不**进 search_failed：通道是辅助渠道，
        # 它的 site: 查询挂掉不影响主链路数据的完整性（2026-11-01 决策 ✓）。
        result.channel_failures = list(self.channel_failures)
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
