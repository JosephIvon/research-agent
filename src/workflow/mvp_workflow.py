"""MVP工作流模块 - 2-agent简化版本：协调者 + 文案撰写员"""
from typing import List, Dict, Optional, Any
from datetime import datetime
from uuid import uuid4
from src.llm import get_llm_client, LLMClient
from src.crawler.web_crawler import WebCrawler
from src.tools.search_tool import SearchTool, get_search_tool
from src.tools.comparison_tool import ComparisonAnalyzer, infer_competitor_name
from src.tools.quality_scorer import QualityScorer
from src.sync import doc_sync_manager
from src.config.settings import (
    VERIFICATION_PASS_THRESHOLD,
    VERIFICATION_WARN_THRESHOLD,
    MAX_RETRIES,
    MAX_FETCH_URLS
)
from src.security.url_validation import UnsafeURL, validate_fetch_url, validate_fetch_urls
import asyncio
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CoordinatorAgent:
    """协调者Agent - 负责任务分解、工具调用、结果整合"""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client
        self.search_tool = get_search_tool()

    async def decompose_task(self, user_query: str, target_urls: List[str]) -> List[Dict[str, str]]:
        """分解用户查询为子任务"""
        prompt = [
            {
                "role": "system",
                "content": """你是一个任务分解专家。请将用户的调研查询分解为具体的子任务。
                输出格式为JSON数组，每个元素包含id、description、priority字段。
                优先级范围1-5，1最高。"""
            },
            {
                "role": "user",
                "content": f"""请分解以下调研任务：{user_query}
                目标URL: {target_urls}"""
            }
        ]

        response = self.llm_client.chat_completion(prompt, temperature=0.1)
        try:
            return json.loads(response)
        except Exception as e:
            logger.warning(f"任务分解JSON解析失败: {e}")
            return [{
                "id": "1",
                "description": "分析目标网站内容",
                "priority": 1
            }]

    async def search_topics(self, user_query: str, num_results: int = 5) -> List[str]:
        """搜索与查询相关的网站URL"""
        try:
            results = self.search_tool.search(user_query, num_results=num_results)
            urls = [result["url"] for result in results]
            logger.info(f"搜索到 {len(urls)} 个相关结果")
            return urls
        except Exception as e:
            logger.error(f"搜索失败: {e}")
            return []

    async def extract_key_info(self, raw_content: str, url: str) -> List[Dict[str, Any]]:
        """从原始内容中提取关键信息"""
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": ["string", "object", "array"]},
                    "confidence": {"type": "number"}
                },
                "required": ["key", "value"]
            }
        }

        prompt = [
            {
                "role": "system",
                "content": f"""你是一个信息提取专家。请从以下网页内容中提取关键信息。
                输出格式必须是有效的JSON，按照schema格式输出。
                schema: {schema}"""
            },
            {
                "role": "user",
                "content": f"""来源: {url}\n内容:\n{raw_content[:8000]}"""
            }
        ]

        response = self.llm_client.chat_completion(prompt, temperature=0.1)
        try:
            data = json.loads(response)
            for item in data:
                item["source_url"] = url
                if "confidence" not in item:
                    item["confidence"] = 0.8
            return data
        except Exception as e:
            logger.warning(f"信息提取JSON解析失败: {e}")
            return [{
                "key": "content_summary",
                "value": raw_content[:2000],
                "source_url": url,
                "confidence": 0.7
            }]

    async def verify_consistency(self, extracted_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """基础一致性核查（合并到协调者，不单独拆agent）"""
        if not extracted_data:
            return {"score": 0.0, "passed": False, "warnings": []}

        confidences = [item.get("confidence", 0.5) for item in extracted_data]
        avg_confidence = sum(confidences) / len(confidences) * 10

        warnings = []
        for item in extracted_data:
            if item.get("confidence", 1.0) < 0.7:
                warnings.append(f"低置信度信息: {item.get('key', 'unknown')}")

        score = round(avg_confidence, 1)

        if score >= VERIFICATION_PASS_THRESHOLD:
            passed = True
        elif VERIFICATION_WARN_THRESHOLD <= score < VERIFICATION_PASS_THRESHOLD:
            passed = True
        else:
            passed = False

        return {
            "score": score,
            "passed": passed,
            "warnings": warnings
        }


class CopywriterAgent:
    """文案撰写员Agent - 接收结构化数据，生成最终报告"""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    def generate_report(
        self,
        user_query: str,
        extracted_data: List[Dict[str, Any]],
        verification_info: Dict[str, Any],
        competitors: Optional[List[Dict[str, Any]]] = None,
        quality_report: Any = None
    ) -> str:
        """生成结构化报告，支持竞品对比"""

        if competitors and len(competitors) >= 2:
            comparison_analyzer = ComparisonAnalyzer(competitors)
            comparison_section = comparison_analyzer.generate_full_comparison_section()

            prompt = [
                {
                    "role": "system",
                    "content": """你是一名专业的市场调研分析师，擅长撰写结构化的竞品分析报告。

【报告结构 - 多竞品对比场景】
## 一、调研概述
- 调研目的、参评竞品列表、调研范围
- 不超过80字

## 二、竞品横向对比（核心章节）
### 2.1 对比概览
- 插入对比表格

### 2.2 差异化分析
- 各竞品独特优势
- 各竞品明显劣势

### 2.3 数据完整性评估
- 哪些竞品数据完整？哪些有缺失？

## 三、各竞品详细分析
### 3.1 [竞品A名称]
### 3.2 [竞品B名称]
（每个竞品一段综合评价）

## 四、选型建议
- 根据[场景]推荐
- 优先考虑因素排序
- 给出可落地建议

【写作规范】
- 使用标准Markdown，不使用HTML标签
- 表格对比优先于文字罗列
- 语言简洁专业，不超过2500字
- 分析客观中立
- **重要：所有引用数据必须标注来源URL**"""
                },
                {
                    "role": "user",
                    "content": f"""调研主题：{user_query}

{comparison_section}

原始数据（请务必为每条数据标注来源）：
{self._format_extracted_data_with_sources(extracted_data[:30])}

可信度评分：{verification_info.get('score', 'N/A')}/10
警告信息：{'；'.join(verification_info.get('warnings', [])[:3]) or '无'}

请生成结构化Markdown报告。"""
                }
            ]
        else:
            prompt = [
                {
                    "role": "system",
                    "content": """你是一名专业的市场调研分析师，擅长撰写结构化的竞品分析报告。

报告结构要求：
1. **调研概述**：简要说明调研目的和范围（不超过100字）
2. **核心发现**：列出3-5个关键点
3. **详细分析**：深入分析各维度（如功能、定价、用户体验等）
4. **结论与建议**：总结分析结果，提出建议

写作要求：
- 语言简洁专业，符合职场报告规范
- 引用数据时注明来源
- 分析要客观中立，突出核心差异
- 输出格式为标准Markdown，不要使用html标签
"""
                },
                {
                    "role": "user",
                    "content": f"""请根据以下调研数据，生成一份专业的竞品分析报告。

调研主题：{user_query}

调研数据（请务必为每条数据标注来源）：
{self._format_extracted_data_with_sources(extracted_data)}

核查信息：可信度评分 {verification_info.get('score', 'N/A')}/10
警告：{'；'.join(verification_info.get('warnings', [])) or '无'}

请输出结构化Markdown报告。
"""
                }
            ]

        response = self.llm_client.chat_completion(prompt, temperature=0.1)

        warning_parts = []
        if verification_info.get('warnings'):
            warning_parts.extend(verification_info['warnings'][:3])

        if quality_report and quality_report.missing_dimensions:
            warning_parts.append(f"数据缺失维度：{'、'.join(quality_report.missing_dimensions[:3])}")

        if warning_parts:
            warning_note = f"""
> ⚠️ **数据质量提示**：{'；'.join(warning_parts)}
"""
            response = warning_note + "\n" + response

        if quality_report:
            quality_section = QualityScorer(quality_report.competitors if hasattr(quality_report, 'competitors') else []).generate_quality_section(report=quality_report)
            response = response + "\n\n" + quality_section

        return response

    def _format_extracted_data_with_sources(self, data: List[Dict[str, Any]]) -> str:
        """格式化提取数据为文本，包含来源URL（用于来源标注）"""
        lines = []
        for item in data:
            key = item.get('key', '')
            value = str(item.get('value', ''))[:200]
            source = item.get('source_url', '')
            if source:
                lines.append(f"- **{key}**: {value}\n  - 来源：[查看原始数据]({source})")
            else:
                lines.append(f"- **{key}**: {value}")
        return '\n'.join(lines)


class MVPWorkflow:
    """MVP工作流 - 2-agent模式：协调者 + 文案撰写员"""

    def __init__(self):
        self.llm_client = get_llm_client()
        self.coordinator = CoordinatorAgent(self.llm_client)
        self.copywriter = CopywriterAgent(self.llm_client)

    async def run(
        self,
        user_query: str,
        target_urls: Optional[List[str]] = None,
        target_sites: Optional[List[Dict[str, Any]]] = None,
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        sync_targets: Optional[List[str]] = None,
        enable_search: bool = True,
        custom_selectors: Optional[Dict[str, List[str]]] = None
    ) -> Dict[str, Any]:
        """执行完整调研流程"""
        initial_sites = self._normalize_target_sites(
            target_urls=target_urls,
            target_sites=target_sites,
            auth_credentials=auth_credentials,
            login_url=login_url,
        )
        initial_urls = [site["url"] for site in initial_sites]
        if len(initial_sites) > MAX_FETCH_URLS:
            raise ValueError(f"目标URL数量不能超过 {MAX_FETCH_URLS} 个")

        state = self._initialize_state(user_query, initial_urls)
        state["target_sites"] = initial_sites
        state["custom_selectors"] = custom_selectors

        try:
            state["status"] = "decomposing"
            state["subtasks"] = await self.coordinator.decompose_task(user_query, state["target_urls"])

            if enable_search and not state["target_urls"]:
                state["status"] = "researching"
                search_urls = await self.coordinator.search_topics(user_query)
                search_target_sites = [
                    {"url": url, "auth_credentials": None, "login_url": None}
                    for url in self._filter_search_urls(search_urls)
                ]
                state["target_sites"].extend(search_target_sites)
                state["target_urls"].extend([site["url"] for site in search_target_sites])
                logger.info(f"自动搜索补充了 {len(search_urls)} 个URL")

            state["status"] = "researching"
            state["competitors"] = await self._fetch_all_competitors(
                state["target_sites"],
                auth_credentials,
                login_url,
                custom_selectors
            )

            for comp in state["competitors"]:
                state["extracted_data"].extend(comp["extracted_data"])
                if comp["status"] == "success":
                    state["raw_search_results"].append({
                        "url": comp["url"],
                        "content": str(comp["extracted_data"]),
                        "timestamp": datetime.now(),
                        "source_type": "web"
                    })

            state["status"] = "verifying"
            verification_result = await self.coordinator.verify_consistency(state["extracted_data"])
            state["verification_score"] = verification_result["score"]
            state["verification_passed"] = verification_result["passed"]
            state["verification_warnings"] = verification_result["warnings"]

            successful_competitors = [c for c in state["competitors"] if c["status"] == "success"]
            state["comparison_passed"] = len(successful_competitors) >= 2

            quality_report = QualityScorer(state["competitors"]).evaluate()
            state["quality_score"] = quality_report.overall_score
            state["quality_grade"] = quality_report.grade
            state["missing_dimensions"] = quality_report.missing_dimensions

            state["status"] = "writing"
            if not state["verification_passed"]:
                state["report_final"] = f"❌ 调研失败：可信度评分 {state['verification_score']}/10 低于阈值，无法生成报告。"
                state["status"] = "failed"
                state["termination_reason"] = "可信度不足"
                return state

            if not quality_report.is_usable:
                state["report_final"] = f"⚠️ 调研数据不足：质量评分 {quality_report.overall_score:.1f}/10（{quality_report.grade}级）\n\n原因：\n"
                state["report_final"] += "\n".join(f"- {r}" for r in quality_report.abandonment_reasons)
                state["report_final"] += "\n\n建议：\n"
                state["report_final"] += "\n".join(f"- {r}" for r in quality_report.recommendations)
                state["status"] = "failed"
                state["termination_reason"] = "数据质量不足"
                return state

            state["report_final"] = self.copywriter.generate_report(
                user_query,
                state["extracted_data"],
                verification_result,
                competitors=state["competitors"],
                quality_report=quality_report
            )

            state["sync_results"]["local_file"] = self._save_report(state["report_final"])

            if sync_targets:
                state = await self._sync_documents(state, sync_targets)

            state["status"] = "completed"
            state["termination_reason"] = "所有步骤执行完成"

        except Exception as e:
            logger.error(f"工作流执行失败: {e}", exc_info=True)
            state["status"] = "failed"
            state["termination_reason"] = f"执行失败: {str(e)}"
            state["errors"].append({
                "timestamp": datetime.now(),
                "stage": "workflow",
                "error_type": type(e).__name__,
                "message": str(e),
                "retry_attempts": 0
            })

        return state

    def _filter_search_urls(self, urls: List[str]) -> List[str]:
        """Keep only safe URLs from search results before the crawler sees them."""
        safe_urls = []
        for url in urls:
            if len(safe_urls) >= MAX_FETCH_URLS:
                break
            try:
                safe_urls.extend(validate_fetch_urls([url]))
            except UnsafeURL as exc:
                logger.warning(f"跳过不安全搜索结果 {url}: {exc}")
        return safe_urls

    def _normalize_target_sites(
        self,
        target_urls: Optional[List[str]] = None,
        target_sites: Optional[List[Dict[str, Any]]] = None,
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sites: List[Dict[str, Any]] = []
        seen_urls = set()
        legacy_login_url = validate_fetch_url(login_url) if login_url else None

        for site in target_sites or []:
            url = validate_fetch_url(site["url"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            sites.append({
                "url": url,
                "auth_credentials": site.get("auth_credentials"),
                "login_url": validate_fetch_url(site["login_url"]) if site.get("login_url") else None,
            })

        for url in validate_fetch_urls(target_urls or []):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            sites.append({
                "url": url,
                "auth_credentials": auth_credentials,
                "login_url": legacy_login_url,
            })

        return sites

    def _initialize_state(self, user_query: str, target_urls: List[str]) -> Dict[str, Any]:
        return {
            "task_id": str(uuid4()),
            "status": "pending",
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
            "termination_reason": None,
            "user_query": user_query,
            "target_urls": target_urls,
            "auth_credentials": None,
            "subtasks": None,
            "current_subtask_index": 0,
            "raw_search_results": [],
            "extracted_data": [],
            "verification_results": [],
            "verification_score": None,
            "verification_passed": None,
            "verification_warnings": [],
            "quality_score": None,
            "quality_grade": None,
            "missing_dimensions": [],
            "competitors": [],
            "comparison_matrix": None,
            "comparison_passed": None,
            "report_draft": None,
            "report_final": None,
            "errors": [],
            "retry_count": 0,
            "max_retries": MAX_RETRIES,
            "sync_targets": [],
            "sync_results": {}
        }

    async def _fetch_all_competitors(
        self,
        sites: List[Any],
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        custom_selectors: Optional[Dict[str, List[str]]] = None
    ) -> List[Dict[str, Any]]:
        """并行抓取多个竞品页面，限制并发数"""
        site_configs = [
            item if isinstance(item, dict) else {
                "url": item,
                "auth_credentials": auth_credentials,
                "login_url": login_url,
            }
            for item in sites
        ]
        if not site_configs:
            return []

        semaphore = asyncio.Semaphore(min(3, len(site_configs)))

        async def _limited_fetch(site: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await self._fetch_single_competitor(
                    site["url"],
                    site.get("auth_credentials"),
                    site.get("login_url"),
                    custom_selectors,
                )

        tasks = [_limited_fetch(site) for site in site_configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        competitors = []
        for i, result in enumerate(results):
            if isinstance(result, dict):
                competitors.append(result)
            else:
                competitors.append({
                    "name": f"竞品{i+1}",
                    "url": site_configs[i]["url"],
                    "extracted_data": [],
                    "status": "failed",
                    "error_message": str(result)
                })

        logger.info(f"竞品抓取完成：成功 {sum(1 for c in competitors if c['status'] == 'success')}/{len(competitors)}")
        return competitors

    async def _fetch_single_competitor(
        self,
        url: str,
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        custom_selectors: Optional[Dict[str, List[str]]] = None
    ) -> Dict[str, Any]:
        """抓取单个竞品页面并提取信息"""
        try:
            async with WebCrawler() as crawler:
                content = await crawler.fetch_page(
                    url, auth_credentials, login_url, custom_selectors
                )
                extracted = await self.coordinator.extract_key_info(content, url)

            name = infer_competitor_name(url, extracted)

            return {
                "name": name,
                "url": url,
                "extracted_data": extracted,
                "status": "success",
                "error_message": None
            }
        except Exception as e:
            logger.warning(f"抓取竞品失败 {url}: {e}")
            return {
                "name": infer_competitor_name(url),
                "url": url,
                "extracted_data": [],
                "status": "failed",
                "error_message": str(e)
            }

    def _save_report(self, content: str) -> str:
        """保存报告到本地文件"""
        from src.config.settings import OUTPUT_DIR
        import os

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"research_report_{timestamp}.md"
        filepath = os.path.join(OUTPUT_DIR, filename)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath

    async def _sync_documents(self, state: Dict[str, Any], sync_targets: List[str]) -> Dict[str, Any]:
        """同步到外部文档平台"""
        title = f"竞品调研报告-{state.get('user_query', '未命名')[:20]}"

        for target in sync_targets:
            try:
                if target == "feishu":
                    result = doc_sync_manager.sync_to_feishu(state["report_final"], title)
                    if "error" in result:
                        raise Exception(result["error"])
                    state["sync_results"]["feishu"] = result.get("url", result.get("document_id", ""))
                elif target == "tencent":
                    result = doc_sync_manager.sync_to_tencent(state["report_final"], title)
                    if "error" in result:
                        raise Exception(result["error"])
                    state["sync_results"]["tencent"] = result.get("url", result.get("document_id", ""))
            except Exception as e:
                state["errors"].append({
                    "timestamp": datetime.now(),
                    "stage": "sync",
                    "error_type": f"{target}_sync_failed",
                    "message": str(e),
                    "retry_attempts": 0
                })
        return state


def run_sync(
    user_query: str,
    target_urls: Optional[List[str]] = None,
    target_sites: Optional[List[Dict[str, Any]]] = None,
    auth_credentials: Optional[Dict[str, str]] = None,
    login_url: Optional[str] = None,
    sync_targets: Optional[List[str]] = None,
    enable_search: bool = True,
    custom_selectors: Optional[Dict[str, List[str]]] = None
) -> Dict[str, Any]:
    """同步执行工作流"""
    return asyncio.run(MVPWorkflow().run(
        user_query=user_query,
        target_urls=target_urls,
        target_sites=target_sites,
        auth_credentials=auth_credentials,
        login_url=login_url,
        sync_targets=sync_targets,
        enable_search=enable_search,
        custom_selectors=custom_selectors,
    ))
