"""四个只读工具的 schema 与 handler 绑定（复用既有实现，不重写）。

Handler 通过 workspace 闭包共享运行期状态：
- fetch_site 把完整页面放进 workspace（LLM 只看 preview，省 token）
- extract_facts 从 workspace 取**全文**逐页 map-reduce 抽取——这是对
  legacy `[:LLM_MAX_CONTENT_CHARS]` 整体截断的结构性替代
- compare_competitors 基于已抽取事实生成对比章节（复用 ComparisonAnalyzer）

模块级 import 是刻意的：测试需要在这些名字上打补丁隔离网络。
"""
import asyncio
import json
import logging
from typing import Any, Dict, List

from src.agent.context import estimate_tokens, map_reduce_extract
from src.agent.tools import Tool, ToolRegistry
from src.crawler.web_crawler import WebCrawler
from src.tools.comparison_tool import ComparisonAnalyzer, infer_competitor_name
from src.tools.search_tool import get_search_tool

logger = logging.getLogger(__name__)

FETCH_PREVIEW_CHARS = 1500   # 喂给 LLM 的首屏预览长度，全文进 workspace
EXTRACT_MAX_PAGES = 5

# 逐页抽取的提示词。<!--intent:extract--> 让 fake provider 可脚本化。
_EXTRACT_SYSTEM = """<!--intent:extract-->
你是一个信息提取专家。请从以下网页内容中提取关键信息。
输出格式为 JSON 数组，每个元素包含 key、value、confidence 字段。
优先使用标准 key：pricing、features、models、target_users、integrations、
api_flow、workflow、limitations、reviews。value 保留可核验事实与数字，
不要概括性形容词。只输出 JSON。"""


def build_default_tools(llm_client, workspace: Dict[str, Any]) -> ToolRegistry:
    """构造一次 react 运行的工具集。llm_client 供 extract_facts 使用。"""
    registry = ToolRegistry()

    # ------------------------------------------------------------------
    # web_search
    # ------------------------------------------------------------------
    async def _web_search(query: str, num_results: int = 5) -> List[Dict[str, Any]]:
        tool = get_search_tool()
        results = await asyncio.to_thread(tool.search, query, num_results)
        return [
            {
                "title": (r.get("title") or "")[:120],
                "url": r.get("url"),
                "snippet": (r.get("content") or r.get("snippet") or "")[:300],
            }
            for r in results[:num_results]
        ]

    registry.add(Tool(
        name="web_search",
        description=(
            "搜索公开网络信息，返回标题/URL/摘要。用于了解主题背景、"
            "寻找竞品官网或资料入口。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，尽量具体"},
                "num_results": {"type": "integer", "description": "返回条数 1-10，默认 5"},
            },
            "required": ["query"],
        },
        handler=_web_search,
        timeout=20.0,
        url_params=(),
    ))

    # ------------------------------------------------------------------
    # fetch_site
    # ------------------------------------------------------------------
    async def _fetch_site(url: str, max_pages: int = 3) -> Dict[str, Any]:
        async with WebCrawler() as crawler:
            profile = await crawler.fetch_site_profile(url, max_pages=max_pages)
        pages = profile.get("pages", [])
        workspace.setdefault("sites", {})[url] = profile
        content = profile.get("content") or ""
        return {
            "url": url,
            "page_count": len(pages),
            "pages": [{"url": p.get("url"), "chars": len(p.get("content") or "")} for p in pages],
            "content_preview": content[:FETCH_PREVIEW_CHARS],
            "content_chars": len(content),
            "hint": "全文已存入工作区；需要细节时调用 extract_facts(url) 做逐页结构化抽取",
        }

    registry.add(Tool(
        name="fetch_site",
        description=(
            "抓取一个网站的首页及高价值内页（默认 3 页），返回内容预览与页面清单。"
            "一次只传一个 URL；完整内容会保留在工作区供 extract_facts 深挖。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的网站 URL（https:// 开头）"},
                "max_pages": {"type": "integer", "description": "站内页数上限 1-5，默认 3"},
            },
            "required": ["url"],
        },
        handler=_fetch_site,
        timeout=90.0,
        url_params=("url",),
    ))

    # ------------------------------------------------------------------
    # extract_facts
    # ------------------------------------------------------------------
    async def _extract_facts(url: str) -> Dict[str, Any]:
        profile = (workspace.get("sites") or {}).get(url)
        if not profile:
            # 前置不满足属于调用方错误：抛出后由注册表归一为 ok=False 的观察结果
            raise ValueError(f"该 URL 尚未抓取，请先调用 fetch_site(url={url!r})")
        pages = profile.get("pages", [])[:EXTRACT_MAX_PAGES]

        async def _extract_page(page_url: str, text: str):
            raw = await llm_client.achat_completion(
                [
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": f"来源: {page_url}\n内容:\n{text}"},
                ],
                temperature=0.1,
            )
            text_result = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            try:
                data = json.loads(text_result.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
            except json.JSONDecodeError:
                return []
            return data if isinstance(data, list) else []

        facts = await map_reduce_extract(pages, _extract_page)
        workspace.setdefault("facts", {})[url] = facts
        return {"url": url, "extracted": len(facts),
                "facts": facts[:20],  # 给 LLM 看前 20 条，全部在 workspace
                "hint": "事实已结构化并带来源，写报告时可直接引用"}

    registry.add(Tool(
        name="extract_facts",
        description=(
            "对已抓取的网站做逐页结构化事实抽取（定价/功能/模型/目标用户等维度），"
            "返回带来源的事实列表。必须先 fetch_site 同一 URL。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "已抓取过的网站 URL"},
            },
            "required": ["url"],
        },
        handler=_extract_facts,
        timeout=120.0,
        url_params=(),  # 不触网，仅查 workspace；网络边界在 fetch_site 的 SSRF 校验
    ))

    # ------------------------------------------------------------------
    # compare_competitors
    # ------------------------------------------------------------------
    def _compare_competitors() -> Dict[str, Any]:
        facts_by_url = workspace.get("facts") or {}
        if len(facts_by_url) < 2:
            raise ValueError("至少需要对 2 个站点执行过 extract_facts 才能对比")
        competitors = []
        for url, facts in facts_by_url.items():
            competitors.append({
                "name": infer_competitor_name(url, facts),
                "url": url,
                "status": "success",
                "extracted_data": facts,
            })
        section = ComparisonAnalyzer(competitors).generate_full_comparison_section()
        workspace["comparison_section"] = section
        return {"competitors": [c["name"] for c in competitors],
                "comparison_chars": len(section)}

    registry.add(Tool(
        name="compare_competitors",
        description=(
            "对所有已抽取事实的竞品生成横向对比章节（对比表/差异化/完整度），"
            "生成内容会拼进最终报告。至少需要 2 个站点先完成 extract_facts。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_compare_competitors,
        timeout=15.0,
        url_params=(),
    ))

    return registry


def system_prompt(tool_schemas: List[Dict[str, Any]], max_steps: int) -> str:
    """ReAct 系统提示词。<!--intent:react--> 供 fake provider 脚本化。"""
    return f"""<!--intent:react-->
你是一名严谨的行业研究员 Agent。通过调用工具收集信息，最终产出可核验的调研结论。

可用工具（JSON Schema）：
{json.dumps(tool_schemas, ensure_ascii=False, indent=2)}

工作规则：
1. 每轮可以调用一个或多个工具；观察结果会以 tool 消息返回给你。
2. 信息足够时，直接输出最终答案（Markdown 报告），不再调用工具。
3. 报告中含数字的结论必须注明来源 URL；没有证据支撑的结论删除整句。
4. 不要重复调用相同工具与相同参数；连续空结果说明方向不对，换思路或收敛。
5. 你最多有 {max_steps} 轮；步数或预算耗尽前必须给出基于现有信息的最佳答案。
"""
