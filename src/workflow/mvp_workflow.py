"""MVP工作流模块 - 2-agent简化版本：协调者 + 文案撰写员"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from datetime import datetime
from uuid import uuid4
from src.llm import get_llm_client, strip_thinking, LLMClient, LLMError
from src.crawler.web_crawler import WebCrawler
from src.tools.search_tool import SearchTool, get_search_tool
from src.tools.comparison_tool import ComparisonAnalyzer, infer_competitor_name
from src.tools.quality_scorer import QualityScorer
from src.sync import doc_sync_manager
from src.config.settings import (
    EXTRACTION_MIN_EVIDENCE_ITEMS,
    EXTRACTION_SOURCE_COVERAGE_MIN,
    MAX_RETRIES,
    MAX_FETCH_URLS,
    CRAWL_MAX_CONCURRENCY,
    CRAWL_MIN_CONCURRENCY,
    CRAWL_REQUEST_INTERVAL,
    MIN_COMPETITORS_FOR_COMPARISON,
    GAP_REFETCH_MAX_PAGES,
    LLM_MAX_CONTENT_CHARS
)
from src.security.url_validation import UnsafeURL, validate_fetch_url, validate_fetch_urls
from src.agent.config import AGENT_TOKEN_BUDGET, AGENT_USD_BUDGET
from src.agent.runner import ReActRunner
from src.agent.tool_specs import build_default_tools
from src.llm.token_budget import TaskBudget
from src.agent.citations import (
    CLAIM_KIND_GAP_ACK,
    EvidenceStore,
    NUMBER_RE,
    format_violations,
    verify_report,
    violation_types,
)
from src.security.injection import (
    UNTRUSTED_NOTICE,
    collect_canaries,
    redact_echo,
    sanitize,
    scan,
    should_drop_page,
    wrap_untrusted,
)
from src.workflow.task_store import _sanitize_payload, TaskEventType
import asyncio
import contextlib
import json
import os
import re
import logging

from src.crawler.concurrency import get_concurrency_controller

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)



def _parse_json_response(response: str):
    """解析 LLM 返回的 JSON。

    要扛住四件真实发生过的事，每一件都是**运行日志里的实测故障**，不是假想：

    1. **推理前缀**：客户端层已剥离，但换客户端实现或模型把推理塞在别处时仍会来。
       不扛住的后果是 JSON 解析失败 → except 兜底成 `content_summary`，
       结构化数据静默全丢还不报错。
    2. **单个 markdown 围栏**：最常见。
    3. **多个围栏**：模型分批吐 JSON，每批各包一个 ```json（实测抽取路径遇到过）。
    4. **`Extra data`**：JSON 解析成功了，后面还有内容——可能是第二批 JSON，
       也可能是一句说明文字。实测 6 次真实抽取里有 1 次栽在这。

    分批输出必须**合并**而不是取第一段：丢掉后半批等于数据损失。
    合并不去重——下游 `EvidenceStore` 按 sha256(url,key,value) 去重，重复项在那里被吸收。
    """
    if not response:
        raise ValueError("Empty response")
    text = strip_thinking(response)
    if not text:
        raise ValueError("Response contained only a thinking block")

    fenced = _fenced_bodies(text)
    if fenced:
        values = [v for v in (_loads_or_none(body) for body in fenced) if v is not None]
        if values:
            return _merge_values(values)

    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        merged = _decode_json_segments(text)
        if merged is None:
            raise first_error
        return merged


def _fenced_bodies(text: str) -> List[str]:
    """抽出所有 ``` 围栏的内容（```json 或裸 ```），没有围栏则返回空列表。"""
    bodies = re.findall(r"```(?:json)?[ \t]*\r?\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    # 老行为兼容：只有开围栏没有闭围栏时，按首尾行剥一次
    if not bodies and text.lstrip().startswith("```"):
        lines = text.strip().split("\n")
        if len(lines) >= 3:
            bodies = ["\n".join(lines[1:-1])]
    return [re.sub(r"^json\s+", "", b, flags=re.IGNORECASE).strip() for b in bodies]


def _loads_or_none(body: str):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _merge_values(values: List[Any]):
    """多个 JSON 值 → 单个返回值。全是数组则拼接，否则取第一个。"""
    if len(values) == 1:
        return values[0]
    if all(isinstance(v, list) for v in values):
        combined: List[Any] = []
        for value in values:
            combined.extend(value)
        return combined
    return values[0]


def _decode_json_segments(text: str):
    """逐段解出文本里的 JSON 值。

    - 全部是数组 → 合并成一个（模型分批输出）
    - 否则       → 取第一个可解出的值（尾部说明文字直接丢弃）

    一段都解不出来时返回 None，让调用方抛原始异常——不做"猜测性降级"。
    """
    decoder = json.JSONDecoder()
    values: List[Any] = []
    idx, length = 0, len(text)
    while idx < length:
        while idx < length and text[idx] in " \t\r\n,;":
            idx += 1
        if idx >= length:
            break
        try:
            value, idx = decoder.raw_decode(text, idx)
        except ValueError:
            break
        values.append(value)
    if not values:
        return None
    return _merge_values(values)


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

        response = await self.llm_client.achat_completion(prompt, temperature=0.1)
        try:
            # 必须走共享解析入口，不能裸 json.loads：模型经常把 JSON 包在
            # ```json 围栏里（提示词没禁止，它就会这么做），裸解析直接
            # `Expecting value: line 1 column 1`，任务分解静默降级成写死的一步。
            # 真实端到端跑出来的实测遗漏。
            return _parse_json_response(response)
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
                schema: {schema}

                请优先使用以下标准 key，便于后续质量评分和报告对比：
                pricing、features、models、target_users、integrations、api_flow、workflow、limitations、reviews。
                value 中要保留可核验的事实、数字、页面来源线索；不要只写概括性形容词。

                {UNTRUSTED_NOTICE}"""
            },
            {
                "role": "user",
                "content": f"来源: {url}\n内容:\n{wrap_untrusted(url, raw_content[:LLM_MAX_CONTENT_CHARS])}"
            }
        ]

        response = await self.llm_client.achat_completion(prompt, temperature=0.1)
        try:
            data = _parse_json_response(response)
            for item in data:
                item["source_url"] = url
            normalized = []
            for item in data:
                if isinstance(item, str):
                    # 模型返回了字符串数组，转为标准格式
                    normalized.append({"key": "content", "value": item, "source_url": url, "confidence": 0.8})
                elif isinstance(item, dict):
                    item["source_url"] = url
                    if "confidence" not in item:
                        item["confidence"] = 0.8
                    normalized.append(item)
                else:
                    normalized.append({"key": "content", "value": str(item), "source_url": url, "confidence": 0.5})
            return normalized
        except Exception as e:
            logger.warning(f"信息提取JSON解析失败: {e}")
            # 兜底路径直接把原文塞进 extracted_data，之后会流入报告提示词。
            # 必须在这里再洗一次：这是唯一绕过 extract_key_info 清洗的路径。
            summary, _ = sanitize(raw_content[:2000])
            return [{
                "key": "content_summary",
                "value": summary,
                "source_url": url,
                "confidence": 0.7
            }]

    async def assess_extracted_data(self, extracted_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """抽取质量评估——只测**可确定性验证**的属性。

        刻意不再对 `confidence` 求平均。那个数是 LLM 给自己的评分：模型没返回时
        由 `extract_key_info` 硬编码成 0.8，所以它衡量的是"模型有多自信"，不是
        "抽出来的东西有多真"。实测旧口径的行为：

            全部 confidence=0.8（空壳数据）  → 8.0 通过
            全部 confidence=0.5（最脏数据）  → 5.0 通过
            空列表                          → 0.0 不通过

        即除了"一条都没有"，什么都放行——那不是门禁，是恒真的存在性检查。

        改为两项真实可测的属性：
        - **来源覆盖率**：多少条数据带得上可追溯的 source_url。没有来源的事实无法被引用校验
        - **证据条数**：够不够写出一份有依据的报告
        模型自评的 confidence 保留为**标注清楚的警告**，不参与门禁。
        """
        if not extracted_data:
            return {
                "score": 0.0,
                "passed": False,
                "source_coverage": 0.0,
                "evidence_count": 0,
                "numeric_evidence_count": 0,
                "warnings": ["没有抽取到任何数据，无法生成报告"],
            }

        total = len(extracted_data)
        with_source = sum(
            1 for item in extracted_data
            if isinstance(item, dict) and str(item.get("source_url", "") or "").strip()
        )
        numeric_count = sum(
            1 for item in extracted_data
            if isinstance(item, dict) and NUMBER_RE.search(str(item.get("value", "") or ""))
        )
        source_coverage = with_source / total

        warnings: List[str] = []
        if source_coverage < EXTRACTION_SOURCE_COVERAGE_MIN:
            warnings.append(
                f"{total - with_source}/{total} 条数据缺少来源链接，无法追溯"
            )
        if numeric_count == 0:
            warnings.append("没有任何一条数据包含数值，报告将无法给出带数字的可核验结论")
        low_confidence = sum(
            1 for item in extracted_data
            if isinstance(item, dict) and float(item.get("confidence", 1.0) or 0) < 0.7
        )
        if low_confidence:
            # 措辞必须写明这是模型自评——它不是客观指标，只是提示抽取可能不稳
            warnings.append(f"{low_confidence} 条数据的抽取置信度偏低（模型自评，非客观指标）")

        passed = (
            source_coverage >= EXTRACTION_SOURCE_COVERAGE_MIN
            and total >= EXTRACTION_MIN_EVIDENCE_ITEMS
        )

        return {
            # score 保留是为了不破坏 SQLite metadata 与接口契约，但其含义已改为
            # 「来源覆盖率 × 10」——它不再代表任何可信度。
            "score": round(source_coverage * 10, 1),
            "passed": passed,
            "source_coverage": round(source_coverage, 3),
            "evidence_count": total,
            "numeric_evidence_count": numeric_count,
            "warnings": warnings,
        }


# 报告生成的 max_tokens 单独放大，不沿用 LLM 客户端的 4096 默认。
#
# 推理模型的 <think> 与正文**共用同一个预算**。实测（MiniMax-M3，同一提示词各 6 次，
# temperature=0.1，夹具证据 4 条）：
#
#   max_tokens=4096  → finish_reason=length 2/6（33%），正文被砍到 954 / 671 字符；
#                      completion_tokens 中位 3792
#   max_tokens=16384 → finish_reason=length 0/6，  completion_tokens 中位 2788
#
# 两点值得注意：
# 1. 被截断的报告**不是空的**——它看起来正常，���只少了结论和建议章节，
#    比"空内容"更隐蔽。客户端的 `cleaned or None` 兜底完全拦不住这种情况。
# 2. 调大预算**不增加成本**：16384 组的中位消耗反而更低（2788 vs 3792）。
#    预算给足后模型不必在硬截断线上挣扎，只是**不被强制中断**。
#
# 样本量 N=6，2/6 vs 0/6 的 Fisher 单侧 p≈0.45，**统计上不显著**；
# 但 `finish_reason="length"` 是服务端给出的硬信号，不是推断出来的。
# 真正确认失败模式的是 smoke4 那次「正文为空」，频率低于本次 12 次采样的检出下限。
REPORT_MAX_TOKENS = 16384


class CopywriterAgent:
    """文案撰写员Agent - 接收结构化数据，生成最终报告"""

    def __init__(self, llm_client: LLMClient):
        self.llm_client = llm_client

    async def generate_report(
        self,
        user_query: str,
        extracted_data: List[Dict[str, Any]],
        verification_info: Dict[str, Any],
        competitors: Optional[List[Dict[str, Any]]] = None,
        quality_report: Any = None,
        evidence_store: Optional[EvidenceStore] = None,
        violations_feedback: str = "",
        max_tokens: Optional[int] = None
    ) -> str:
        """生成结构化报告，支持竞品对比

        `evidence_store` 由调用方传入，以便报告生成完后用**同一份**证据做校验——
        这里如果自己重建，两边的 id 可能对不上，校验就会全量报 unknown_citation。

        `max_tokens` 单独暴露：推理模型的 `<think>` 与正文**共用同一个预算**，
        预算给小了会出现"推理吃满、正文被硬截断"的残缺报告（实测 4096 下 2/6 被
        finish_reason=length 砍掉一半正文）。传 None 表示用 `REPORT_MAX_TOKENS`，
        不是客户端默认值——客户端默认太小。
        """
        evidence_store = evidence_store or EvidenceStore.from_extracted_data(extracted_data)
        evidence_block = (
            f"可引用证据表（共 {len(evidence_store)} 条）：\n\n{evidence_store.table()}"
        )
        feedback_block = f"\n\n{violations_feedback}" if violations_feedback else ""

        if competitors and len(competitors) >= MIN_COMPETITORS_FOR_COMPARISON:
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

## 四、API / 数据流 / 工作流对比
- 对比注册、获取 Key、替换 Base URL、调用模型、监控用量、计费结算等链路
- 区分“网页明确披露”和“未公开披露”，不要脑补

## 五、选型建议
- 根据[场景]推荐
- 优先考虑因素排序
- 给出可落地建议

【写作规范】
- 使用标准Markdown，不使用HTML标签
- 表格对比优先于文字罗列
- 语言简洁专业，不超过2500字
- 分析客观中立
- **重要：凡是含数字（金额、数量、比例、版本号）的结论句，必须在该句末尾标注证据标记 `[e:xxxx]`，标记只能从下方证据表里原样复制。**
- 没有证据支撑的结论，**整句删除**——不要保留一个没有来源的断言，也不要编造标记。""",
                },
                {
                    "role": "user",
                    "content": f"""调研主题：{user_query}

{comparison_section}

{evidence_block}

数据可追溯性：来源覆盖率 {verification_info.get('source_coverage', 0):.0%}，证据 {verification_info.get('evidence_count', 0)} 条
警告信息：{'；'.join(verification_info.get('warnings', [])[:3]) or '无'}

请生成结构化Markdown报告。{feedback_block}"""
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
4. **API / 数据流 / 工作流分析**：说明用户从注册、获取 Key、替换 Base URL、调用模型、监控用量到计费结算的链路；若网页没有证据，要明确标注“未公开披露”
5. **结论与建议**：总结分析结果，提出建议

写作要求：
- 语言简洁专业，符合职场报告规范
- 分析要客观中立，突出核心差异
- 输出格式为标准Markdown，不要使用html标签
- **凡是含数字（金额、数量、比例、版本号）的结论句，必须在该句末尾标注证据标记 `[e:xxxx]`，标记只能从下方证据表里原样复制。**
- 没有证据支撑的结论，**整句删除**，不要编造标记
"""
                },
                {
                    "role": "user",
                    "content": f"""请根据以下调研数据，生成一份专业的竞品分析报告。

调研主题：{user_query}

{evidence_block}

数据可追溯性：来源覆盖率 {verification_info.get('source_coverage', 0):.0%}，证据 {verification_info.get('evidence_count', 0)} 条
警告：{'；'.join(verification_info.get('warnings', [])) or '无'}

请输出结构化Markdown报告。{feedback_block}"""
                }
            ]

        call_kwargs = {"temperature": 0.1, "max_tokens": max_tokens or REPORT_MAX_TOKENS}
        response = await self.llm_client.achat_completion(prompt, **call_kwargs)

        if not response or not str(response).strip():
            # 推理模型可能把 max_tokens 全花在 <think> 上，正文一个字都没输出，
            # 剥离层于是返回 None。这里必须显式失败而不是让空字符串流下去：
            # 空报告会被 verify_report 平凡判为"四指标全过"（claims=[] → coverage=1.0），
            # 然后顶着"引用校验通过"的标签交付给用户——静默失败比报错危险得多。
            raise LLMError(
                "报告生成返回空内容：模型输出只有推理块（可能被 max_tokens 截断），"
                "没有得到正文"
            )

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
        """格式化提取数据为文本，包含来源URL（用于来源标注）

        这是**二阶注入面**：extracted_data 是 LLM 的输出，但它可能复述抓取页面里的
        攻击文本（尤其是 extract_key_info 的兜底路径）。报告生成提示词同样要过一道清洗，
        否则攻击者可以用「提取 → 复述 → 成文」绕过入口防护。
        """
        lines = []
        for item in data:
            key = item.get('key', '')
            value = str(item.get('value', ''))[:200]
            value, _ = sanitize(value)
            source = item.get('source_url', '')
            if source:
                lines.append(f"- **{key}**: {value}\n  - 来源：[查看原始数据]({source})")
            else:
                lines.append(f"- **{key}**: {value}")
        return '\n'.join(lines)


class MVPWorkflow:
    """MVP工作流 - 2-agent模式：协调者 + 文案撰写员"""

    def __init__(self, event_emitter=None, *,
                 llm_client: Optional[LLMClient] = None,
                 crawler_factory=None,
                 search_factory=None,
                 url_validator=None,
                 token_budget: Optional[int] = None,
                 agent_mode: Optional[str] = None):
        """依赖注入口：全部可选，None 时走生产默认。

        默认值必须在**方法体内**运行时解析模块属性（`WebCrawler`、
        `validate_fetch_url` 等都是 from-import 名字）：大量测试用
        `monkeypatch.setattr(mvp_workflow, ...)` 打模块属性，def 默认参数
        会在定义时固化原始引用，patch 就失效了。

        评测（evals）改用这组参数构造与线上一致的工作流，替代运行期
        monkeypatch——补丁面曾是隐式契约：每新增一个网络边界，都要同步
        改 evals 的 patch 列表，漏一处就是假绿。
        """
        # None 哨兵而不是 `crawler_factory=WebCrawler`：见 docstring，运行时解析
        self.llm_client = llm_client if llm_client is not None else get_llm_client()
        self.coordinator = CoordinatorAgent(self.llm_client)
        self.copywriter = CopywriterAgent(self.llm_client)
        self._event_emitter = event_emitter
        # 存原始注入值（None=未注入）：legacy 管线经 crawler_factory property
        # 运行时解析默认；react 工具链只在显式注入时才覆盖 tool_specs 侧默认
        self._crawler_factory = crawler_factory
        self._search_factory = search_factory
        self._token_budget = token_budget  # None → 使用点运行时读 AGENT_TOKEN_BUDGET
        self._agent_mode = agent_mode      # None → 使用点运行时读 AGENT_MODE 环境变量
        self._validate_url = url_validator if url_validator is not None else validate_fetch_url
        self._url_validator_injected = url_validator is not None
        if url_validator is None:
            self._validate_urls = validate_fetch_urls
        else:
            # 注入单值校验器时批量退化为逐个过闸；生产路径仍走原生批量实现
            self._validate_urls = lambda urls: [url_validator(u) for u in (urls or [])]

    @property
    def crawler_factory(self):
        """legacy 管线的爬虫工厂：显式注入优先；否则**运行时**读模块属性
        WebCrawler——`monkeypatch.setattr(mvp_workflow, "WebCrawler", ...)`
        的既有测试依赖这一点，def 默认参数会在定义时固化原始引用。

        getattr 兜底：部分单测用 object.__new__ 构造部分初始化实例
        （绕过 LLM 客户端构造），新属性在那些实例上不存在。
        """
        injected = getattr(self, "_crawler_factory", None)
        return injected if injected is not None else WebCrawler

    def _state(self) -> Dict[str, Any]:
        """取当前 run 的 state。_fetch_single_competitor 也可能被独立调用，
        那时 self.state 可能还没建立。"""
        state = getattr(self, "state", None)
        if state is None:
            state = {}
            self.state = state
        return state

    def _record_injection_findings(self, url: str, findings: List[Any]) -> None:
        """把注入嫌疑记入 state，供审计与后续 UI 展示。

        只记 rule_id/severity/sample，不记原文偏移——偏移对跨轮次清洗没有意义，
        且避免把攻击者控制的文本带进持久化层。
        """
        if not findings:
            return
        state = getattr(self, "state", None)
        if state is None:
            return
        for finding in findings:
            state.setdefault("injection_findings", []).append({
                "url": url,
                "rule_id": finding.rule_id,
                "severity": finding.severity,
                "sample": finding.sample,
            })
        high = sum(1 for f in findings if f.severity == "high")
        if high:
            self._emit(
                "injection_detected", "crawl", "running",
                f"检测到 {len(findings)} 处注入嫌疑（高危 {high}），已中和",
                payload={
                    "url": url,
                    "finding_count": len(findings),
                    "high_count": high,
                    "rule_ids": sorted({f.rule_id for f in findings}),
                },
            )

    def _mark_low_trust(self, report: str, citation_result: Dict[str, Any]) -> str:
        """在报告顶部挂出低可信标记与具体违规项。

        不静默交付是硬要求：一份没通过引用校验的报告如果照常呈现，
        用户会把它当成可信结论——这比直接报错更危险。

        校验通过时原样返回。守卫放在函数内部而不是只靠调用方判断——
        否则一旦有人把 run() 里的 else 分支去掉，通过的报告也会被挂上低可信标记。
        """
        if citation_result.get("passed"):
            return report

        violations = citation_result.get("violations") or []
        # 无引用句分两类呈现：**真·无据断言**是系统该指控的；**已识别缺口**是模型在
        # 诚实标注"这项查不到"，把它和指控并列会让用户误以为报告在编造。
        # 两类都仍然是违规、都仍然拉低了覆盖率——这里只改读起来像什么。
        accusations = [v for v in violations
                       if v.get("type") == "uncited_claim"
                       and v.get("kind") == CLAIM_KIND_GAP_ACK]
        real = [v for v in violations if v not in accusations]

        head = (
            "> ⚠️ **低可信标记：本文未通过引用完整性校验**",
            f"> 引用覆盖 {citation_result['citation_coverage']:.0%}｜"
            f"扎根率 {citation_result['grounding']:.0%}｜"
            f"问题 {len(violations)} 处"
            "（已自动重生成一次仍未通过）",
        )
        if accusations:
            head = head[:-1] + (
                f"> 其中 {len(accusations)} 处是**已主动标注的信息缺口**，"
                "不是编造——这些内容公开渠道确实查不到",
                ">",
            )

        lines = list(head)
        if real:
            lines.append("> 需要修正：")
            for item in real[:8]:
                lines.append(f"> - [{item.get('type')}] {item.get('detail')}")
            if len(real) > 8:
                lines.append(f"> - ……另有 {len(real) - 8} 处")
        else:
            lines.append("> 无需修正：全部问题均为已标注的信息缺口。")

        if accusations:
            lines.append(">")
            lines.append("> 已标注的信息缺口（原文如实说明了查不到，不要求补引用）：")
            for item in accusations[:4]:
                lines.append(f"> - {item.get('detail')}")
            if len(accusations) > 4:
                lines.append(f"> - ……另有 {len(accusations) - 4} 处")

        lines.append("")
        lines.append("---")
        lines.append("")
        return "\n".join(lines) + report

    def _enforce_report_output(self, state: Dict[str, Any]) -> None:
        """报告出口的回声校验。

        清洗后的页面理论上不该让注入文本抵达报告。这里是**独立**的第二道闸：
        它不依赖清洗是否生效，只看最终报告里是否原样出现了抓取页面中的探针。
        命中即说明清洗被绕过（或该串另有来源），必须从交付物里移除。
        """
        if not self._state().get("injection_canaries"):
            return
        cleaned, hits = redact_echo(
            state.get("report_final") or "", self._state()["injection_canaries"]
        )
        if not hits:
            return

        logger.error(f"最终报告中检出注入回声 {len(hits)} 处，已移除")
        state["report_final"] = cleaned
        state.setdefault("injection_findings", []).extend(
            {"url": "", "rule_id": "report_echo", "severity": "high", "sample": h[:60]}
            for h in hits
        )
        self._emit(
            "injection_detected", "report_generate", "completed",
            f"报告中检出注入回声 {len(hits)} 处，已移除",
            payload={"finding_count": len(hits), "rule_ids": ["report_echo"], "high_count": len(hits)},
        )

    def _emit(self, event_type: str, stage: str, status: str, message: str, payload: Optional[Dict[str, Any]] = None):
        # 事件名对表：TaskEventType 是 SSE 事件名的唯一登记处。裸字符串打错一个
        # 字母，前端只会安静地少收一个事件——没有任何报错能告诉你。
        # 对表放在最前面（在"没有 emitter 就早退"之前），免得没订阅者时
        # 打错的名字在测试里被完全掩盖过去。
        from src.workflow.task_store import KNOWN_EVENT_NAMES, TaskEvent
        if event_type not in KNOWN_EVENT_NAMES:
            raise ValueError(
                f"未登记的事件名 {event_type!r}：请先在 TaskEventType 里加成员，"
                "并确认前端 BACKEND_EVENTS 订阅了它"
            )
        if self._event_emitter is None:
            return
        event = TaskEvent(
            id=0,
            event=event_type,
            task_id=self.state.get("task_id", ""),
            stage=stage,
            status=status,
            message=message,
            payload=_sanitize_payload(payload or {}),
        )
        asyncio.create_task(self._event_emitter.emit(event))

    async def run(
        self,
        user_query: str,
        target_urls: Optional[List[str]] = None,
        target_sites: Optional[List[Dict[str, Any]]] = None,
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        sync_targets: Optional[List[str]] = None,
        enable_search: bool = True,
        custom_selectors: Optional[Dict[str, List[str]]] = None,
        deliverables: Optional[Dict[str, bool]] = None,
        task_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        deliverables = deliverables or {"report": True, "prd": False}

        initial_sites = self._normalize_target_sites(
            target_urls=target_urls,
            target_sites=target_sites,
            auth_credentials=auth_credentials,
            login_url=login_url,
        )
        initial_urls = [site["url"] for site in initial_sites]
        if len(initial_sites) > MAX_FETCH_URLS:
            raise ValueError(f"目标URL数量不能超过 {MAX_FETCH_URLS} 个")

        state = self._initialize_state(user_query, initial_urls, task_id, user_id)
        state["target_sites"] = initial_sites
        state["custom_selectors"] = custom_selectors
        self.state = state

        # AGENT_MODE=react：切换到 ReAct 工具循环（模型自主规划抓取/抽取/对比），
        # 以下是 legacy 固定管线，路径保持不动。env 运行时读取，方便部署侧切换；
        # 评测用 agent_mode="react" 显式注入，不再借道环境变量。
        mode = self._agent_mode if self._agent_mode is not None else os.getenv("AGENT_MODE", "legacy")
        if mode.lower() == "react":
            return await self._run_react_mode(state, user_query, state["target_urls"])

        try:
            state["status"] = "decomposing"
            self._emit("decompose", "decompose", "running", "正在理解需求、拆解调研维度")
            state["subtasks"] = await self.coordinator.decompose_task(user_query, state["target_urls"])

            if enable_search and not state["target_urls"]:
                state["status"] = "researching"
                self._emit("search", "search", "running", "正在搜索竞品范围和公开资料")
                search_urls = await self.coordinator.search_topics(user_query)
                search_target_sites = [
                    {"url": url, "auth_credentials": None, "login_url": None}
                    for url in self._filter_search_urls(search_urls)
                ]
                state["target_sites"].extend(search_target_sites)
                state["target_urls"].extend([site["url"] for site in search_target_sites])
                logger.info(f"自动搜索补充了 {len(search_urls)} 个URL")

            state["status"] = "crawling"
            self._emit("crawl_start", "crawl", "running", f"开始登录/抓取 {len(state['target_sites'])} 个竞品网站")
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

            success_count = sum(1 for c in state["competitors"] if c["status"] == "success")
            failed_count = sum(1 for c in state["competitors"] if c["status"] == "failed")
            blocked_count = sum(1 for c in state["competitors"] if c["status"] == "blocked")
            self._emit(
                "crawl_complete", "crawl", "completed",
                f"抓取完成：成功 {success_count}，失败 {failed_count}，注入拦截 {blocked_count}",
                payload={
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "blocked_count": blocked_count,
                }
            )

            state["status"] = "extracting"
            self._emit("extract", "extract", "running", "正在提取功能、定价、模型、目标用户、限制等信息")
            # Extract is implicit in the crawl data, nothing extra to call

            state["status"] = "verifying"
            self._emit("verify", "verify", "running", "正在检查抽取数据的来源可追溯性")
            verification_result = await self.coordinator.assess_extracted_data(state["extracted_data"])
            state["verification_score"] = verification_result["score"]
            state["verification_passed"] = verification_result["passed"]
            state["verification_warnings"] = verification_result["warnings"]
            state["source_coverage"] = verification_result["source_coverage"]
            state["evidence_count"] = verification_result["evidence_count"]
            state["numeric_evidence_count"] = verification_result["numeric_evidence_count"]

            successful_competitors = [c for c in state["competitors"] if c["status"] == "success"]
            state["comparison_passed"] = len(successful_competitors) >= MIN_COMPETITORS_FOR_COMPARISON

            quality_report = QualityScorer(state["competitors"]).evaluate()
            state["quality_score"] = quality_report.overall_score
            state["quality_grade"] = quality_report.grade
            state["missing_dimensions"] = quality_report.missing_dimensions

            state["status"] = "writing"
            if not state["verification_passed"]:
                # 门禁现在只看两项可确定性验证的属性：来源覆盖率与证据条数。
                # 绝不能再称作"可信度"——可信度由引用校验负责，这里管的是数据能不能追溯。
                state["report_final"] = (
                    f"❌ 调研中止：来源覆盖率 {state['source_coverage']:.0%}、"
                    f"证据条数 {state['evidence_count']}，未达生成报告的最低要求。\n\n"
                    + "\n".join(f"- {w}" for w in state["verification_warnings"])
                    + "\n\n注：本项只衡量数据是否可追溯，**不代表结论正确性**。"
                )
                state["status"] = "failed"
                state["termination_reason"] = "抽取数据不足或缺来源"
                # blocked 快照：门禁失败即中止（闭环末到不了 readiness），
                # 在此落统一裁决观测，让前端/日志/评测看到一致的 blocked 出口
                state["delivery_readiness"] = {
                    "verdict": "blocked",
                    "hard_gates": [
                        f"来源覆盖率 {state['source_coverage']:.0%}、证据条数 "
                        f"{state['evidence_count']}，未达生成报告的最低要求",
                    ],
                    "reasons": ["硬性条件不满足，中止交付"],
                    "dimensions": {}, "critical_gaps": [], "actionable_gap": False,
                }
                self._emit(
                    "error", "verify", "failed",
                    f"抽取数据不足：来源覆盖率 {state['source_coverage']:.0%}、"
                    f"证据 {state['evidence_count']} 条",
                )
                state["injection_findings_count"] = len(state.get("injection_findings") or [])
                return state

            if not quality_report.is_usable:
                state["report_final"] = f"⚠️ 调研数据不足：数据完整度 {quality_report.overall_score:.1f}/10（{quality_report.grade}级）\n\n原因：\n"
                state["report_final"] += "\n".join(f"- {r}" for r in quality_report.abandonment_reasons)
                state["report_final"] += "\n\n建议：\n"
                state["report_final"] += "\n".join(f"- {r}" for r in quality_report.recommendations)
                state["status"] = "failed"
                state["termination_reason"] = "数据质量不足"
                # blocked 快照（同上）：quality 门禁失败的中止出口
                state["delivery_readiness"] = {
                    "verdict": "blocked",
                    "hard_gates": [f"数据完整度 {quality_report.overall_score:.1f}/10（{quality_report.grade}级）"],
                    "reasons": ["硬性条件不满足，中止交付"],
                    "dimensions": {}, "critical_gaps": [], "actionable_gap": False,
                }
                self._emit("error", "verify", "failed", f"数据质量不足：{quality_report.overall_score}/10")
                state["injection_findings_count"] = len(state.get("injection_findings") or [])
                return state

            state["status"] = "report_generating"
            self._emit("report_generate", "report_generate", "running", "正在生成竞品分析报告")
            evidence_store = EvidenceStore.from_extracted_data(state["extracted_data"])
            state["evidence_store"] = evidence_store
            state["report_final"] = await self.copywriter.generate_report(
                user_query,
                state["extracted_data"],
                verification_result,
                competitors=state["competitors"],
                quality_report=quality_report,
                evidence_store=evidence_store,
            )

            # --- 引用校验闭环：不过则带违规清单重生成一次，仍不过标记低可信 ---
            state["status"] = "citation_check"
            self._emit(
                "citation_check", "verify", "running",
                f"正在校验引用完整性（证据 {len(evidence_store)} 条）",
            )
            citation_result = await asyncio.to_thread(
                verify_report, state["report_final"], evidence_store
            )
            state["citation_attempts"] = 1

            # decision 供「预算合并」计算（维度补抓共享 GAP_REFETCH_MAX_PAGES）：
            # 必须在 861 的 if 块外初始化，否则第一次校验直接通过时未定义。
            decision = None

            if not citation_result["passed"]:
                # 第一次校验的违规明细会在第二次 verify 之后被覆盖（见下方
                # 的 state["citation_violations"] 赋值）。重生成成功与否决定了这个
                # 任务有没有走过「校验不过 → 补救」这条路径，而这批类型是事后唯一
                # 能回答「它当初为什么被重生成」的线索——不留档就永远查不到了。
                state["citation_first_attempt"] = {
                    "violation_count": citation_result["violation_count"],
                    "violation_types": violation_types(citation_result["violations"]),
                }
                self._emit(
                    "citation_check", "verify", "running",
                    f"引用校验未通过（{citation_result['violation_count']} 处），带违规清单重生成",
                    payload={"violation_count": citation_result["violation_count"]},
                )
                feedback = format_violations(citation_result)
                state["status"] = "report_regenerating"
                state["report_final"] = await self.copywriter.generate_report(
                    user_query,
                    state["extracted_data"],
                    verification_result,
                    competitors=state["competitors"],
                    quality_report=quality_report,
                    evidence_store=evidence_store,
                    violations_feedback=feedback,
                )
                state["citation_attempts"] = 2
                citation_result = await asyncio.to_thread(
                    verify_report, state["report_final"], evidence_store
                )

                # 第二次校验仍不过 → 判定是否定向补抓（只处理真·证据缺口）。
                # 闭环顺序固定为「补抓 → 带新证据重生成 → 第三次校验」：
                # 只重建证据库不重生成，报告里 [e:id] 仍指向旧证据，第三次校验
                # 必然不过——补抓的产出必须进入报告模型可见的证据表才有意义。
                if not citation_result["passed"]:
                    state["citation_violations"] = citation_result["violations"]
                    grounding_before = citation_result["grounding"]
                    decision = await self._maybe_refetch_for_gaps(state, evidence_store)
                    if decision is not None:
                        evidence_store = EvidenceStore.from_extracted_data(state["extracted_data"])
                        state["evidence_store"] = evidence_store
                        state["status"] = "report_regenerating"
                        self._emit(
                            "citation_check", "verify", "running",
                            f"补抓 {len(decision.candidates)} 个竞品后重新生成报告"
                            f"（证据 {len(evidence_store)} 条）",
                        )
                        quality_report = self._refresh_quality_report(state)
                        state["report_final"] = await self.copywriter.generate_report(
                            user_query,
                            state["extracted_data"],
                            verification_result,
                            competitors=state["competitors"],
                            quality_report=quality_report,
                            evidence_store=evidence_store,
                        )
                        state["citation_attempts"] = 3
                        citation_result = await asyncio.to_thread(
                            verify_report, state["report_final"], evidence_store
                        )
                        # 补抓前后的扎根率：「缺口解决」的裁决信号是 grounding 提升
                        # （refetchable_gap_count 只观测数字是否进证据，不代表报告
                        # 引用了它——gap_refetch 夹具证明：99 缺口、补抓带回 79、
                        # 报告改用 79 才转绿，数字进证据 ≠ 缺口解决）。
                        state.setdefault("gap_refetch_result", {})
                        state["gap_refetch_result"]["grounding_before"] = grounding_before
                        state["gap_refetch_result"]["grounding_after"] = citation_result["grounding"]

            # 预算合并（拍板口径）：GAP_REFETCH_MAX_PAGES 是数值+维度两类补抓的
            # **共享总预算**，不是各自独立。数值补抓已分配页数（per_candidate_pages
            # 之和，承诺抓取量、失败不退还——保守）从总预算扣除，剩余给维度补抓。
            refetch_used_pages = (
                sum(c.per_candidate_pages for c in decision.candidates)
                if decision is not None else 0
            )
            remaining_budget = max(0, GAP_REFETCH_MAX_PAGES - refetch_used_pages)

            # 维度缺口补抓（新分支）：报告**没写**的核心维度不产生违规、判定层
            # 看不到——引用校验通过后核心维度仍 missing/unconfirmed 时，用维度
            # 意图选页补抓，重生成让报告**新增**该维度断言。与数值补抓同轮，
            # 一轮不循环；数值补抓已走过的站点也可能在此补齐缺失维度。
            #
            # **必须放在数值补抓块（上面的 if not citation_result["passed"]）之外。**
            # 原因链（实测踩过）：
            # 1. 数值补抓块被包在「第一次校验不过」的 if 里，只有报告产生违规才执行；
            # 2. 维度缺口的特征恰恰是**无违规**——报告没写定价，就没有 ungrounded
            #    claim 可判，第一次校验**直接通过**（attempts=1、violations=[]）；
            # 3. 若维度块也放进该 if 块内，第一次校验通过时整块被跳过，维度缺口
            #    永远无法触发补抓（实测：dimension_refetch_decision=None、报告
            #    始终缺定价）——这是"新分支接在现有 if 里被意外跳过"的典型坑。
            # 所以维度块与校验结果解耦，每次闭环都执行；补抓与否由
            # plan_dimension_refetch 的 pure 判定决定，不依赖本次校验是否通过。
            dim_grounding_before = citation_result["grounding"]
            dim_decision = await self._maybe_refetch_dimensions(
                state, evidence_store, remaining_budget=remaining_budget,
            )
            if dim_decision is not None:
                evidence_store = EvidenceStore.from_extracted_data(state["extracted_data"])
                state["evidence_store"] = evidence_store
                state["status"] = "report_regenerating"
                self._emit(
                    "citation_check", "verify", "running",
                    f"补抓 {len(dim_decision.candidates)} 个竞品的缺失维度后重新生成报告"
                    f"（证据 {len(evidence_store)} 条）",
                )
                quality_report = self._refresh_quality_report(state)
                state["report_final"] = await self.copywriter.generate_report(
                    user_query,
                    state["extracted_data"],
                    verification_result,
                    competitors=state["competitors"],
                    quality_report=quality_report,
                    evidence_store=evidence_store,
                )
                state["citation_attempts"] = state.get("citation_attempts", 1) + 1
                citation_result = await asyncio.to_thread(
                    verify_report, state["report_final"], evidence_store
                )
                # 「缺口解决」同数值补抓：grounding 提升是裁决信号
                state.setdefault("dimension_refetch_result", {})
                state["dimension_refetch_result"]["grounding_before"] = dim_grounding_before
                state["dimension_refetch_result"]["grounding_after"] = citation_result["grounding"]

            state["citation_report"] = {
                k: v for k, v in citation_result.items() if k != "violations"
            }
            state["citation_violations"] = citation_result["violations"]
            state["citation_trusted"] = citation_result["passed"]

            # 交付就绪统一裁决：把三个散落闸门（verify/quality/引用校验）收敛成
            # 可测试的四态裁决，落 state 供评测/日志/前端观测。
            # refetch_eligible 只在「尚未补抓」时反映判定层缺口——补抓只做一轮，
            # grounding 提升与否已由 gap_refetch_result 记录，不因同一缺口再裁决继续。
            from src.agent.readiness import assess_delivery_readiness
            refetch_done = state.get("gap_refetch_result") is not None
            refetch_eligible = (
                (state.get("gap_refetch_decision") or {}).get("eligible", False)
                and not refetch_done
            )
            state["delivery_readiness"] = assess_delivery_readiness(
                evidence_store=evidence_store,
                citation_passed=citation_result["passed"],
                violations=citation_result["violations"],
                source_coverage=state.get("source_coverage"),
                evidence_count=state.get("evidence_count"),
                verification_passed=state.get("verification_passed"),
                quality_usable=quality_report.is_usable if quality_report else None,
                refetch_eligible=refetch_eligible,
            ).to_dict()

            if citation_result["passed"]:
                self._emit(
                    "citation_check", "verify", "completed",
                    f"引用校验通过：覆盖 {citation_result['citation_coverage']:.0%}、"
                    f"扎根 {citation_result['grounding']:.0%}",
                    payload={
                        "citation_coverage": citation_result["citation_coverage"],
                        "grounding": citation_result["grounding"],
                        "attempts": state["citation_attempts"],
                    },
                )
            else:
                # 不静默交付：报告顶部必须挂出低可信标记与具体违规项
                self._emit(
                    "citation_check", "verify", "failed",
                    f"引用校验未通过：{citation_result['violation_count']} 处问题，报告标记为低可信",
                    payload={
                        "violation_count": citation_result["violation_count"],
                        "attempts": state["citation_attempts"],
                    },
                )
                state["report_final"] = self._mark_low_trust(state["report_final"], citation_result)

            # 交付前最后一道闸：注入文本不得原样出现在报告里
            self._enforce_report_output(state)

            state["sync_results"]["local_file"] = self._save_report(state["report_final"], state.get("task_id"))
            state["report_id"] = Path(state["sync_results"]["local_file"]).stem

            # Persist report metadata to SQLite
            self._persist_to_sqlite(state)

            self._emit(
                "artifact_ready", "report_generate", "completed",
                "竞品分析报告已生成",
                payload={"artifact_type": "report", "report_id": state["report_id"]}
            )

            prd_content = None
            if deliverables.get("prd"):
                from src.tools.prd_generator import PRDGenerator
                state["status"] = "prd_generating"
                self._emit("prd_generate", "prd_generate", "running", "正在生成 PRD")
                generator = PRDGenerator()
                prd_content = await asyncio.to_thread(
                    generator.generate_from_competitive_report,
                    state["report_final"],
                    user_query,
                )
                state["prd_content"] = prd_content
                self._emit(
                    "artifact_ready", "prd_generate", "completed",
                    "PRD 已生成",
                    payload={"artifact_type": "prd", "content": prd_content[:500] if prd_content else ""}
                )

            if sync_targets:
                state = await self._sync_documents(state, sync_targets)

            state["status"] = "completed"
            state["termination_reason"] = "所有步骤执行完成"
            self._emit("completed", "completed", "completed", "全部完成")
            self._update_task_status_in_sqlite(state)

        except Exception as e:
            logger.error(f"工作流执行失败: {e}", exc_info=True)
            state["status"] = "failed"
            state["termination_reason"] = f"执行失败: {str(e)}"
            self._update_task_status_in_sqlite(state)
            state["errors"].append({
                "timestamp": datetime.now(),
                "stage": "workflow",
                "error_type": type(e).__name__,
                "message": str(e),
                "retry_attempts": 0
            })
            self._emit(
                "error", "workflow", "failed",
                f"执行失败: {str(e)}",
                payload={"stage": "workflow", "error_type": type(e).__name__}
            )

        # 收尾统一记录注入审计计数：正常完成与异常兜底两条路径共用这个出口
        state["injection_findings_count"] = len(state.get("injection_findings") or [])
        return state

    async def _run_react_mode(
        self,
        state: Dict[str, Any],
        user_query: str,
        target_urls: List[str],
    ) -> Dict[str, Any]:
        """AGENT_MODE=react：ReAct 工具循环替代 legacy 固定管线。

        Runner 自主决定调用哪些工具（web_search / fetch_site / extract_facts /
        compare_competitors），结束后把 workspace 里的结构化事实接入与 legacy
        相同的交付链：引用校验 → 报告落盘 → SQLite 持久化 → completed。
        """
        state["status"] = "reacting"
        self._emit("agent_step", "agent", "running", "ReAct 模式启动，模型自主规划工具调用")

        try:
            budget = TaskBudget(
                state["task_id"],
                max_tokens=self._token_budget if self._token_budget is not None else AGENT_TOKEN_BUDGET,
                max_usd=AGENT_USD_BUDGET,
            )
            workspace: Dict[str, Any] = {}

            def _on_step(step) -> None:
                # 每轮工具调用实时外发进度，SSE 订阅者据此看到模型正在做什么
                tool_names = "、".join(tc.get("name") or "?" for tc in step.tool_calls) or "（无工具调用）"
                self._emit(
                    "agent_step", "agent", "running",
                    f"第 {step.index} 轮：{tool_names}",
                    payload={
                        "step": step.index,
                        "thought": step.thought,
                        "tool_calls": step.tool_calls,
                        "stop_reason": getattr(step, "stop_reason_if_any", None),
                    },
                )

            # 网络边界只在**显式注入**时才覆盖 react 工具链的默认（tool_specs
            # 侧的 WebCrawler / get_search_tool / ToolRegistry 默认校验器）——
            # 无条件传会把 workflow 级默认压过 tool_specs 侧的 monkeypatch，
            # 两套替换点本就服务不同场景：legacy 管线看 workflow，react 看 tool_specs。
            tool_kwargs: Dict[str, Any] = {}
            injected_crawler = getattr(self, "_crawler_factory", None)
            if injected_crawler is not None:
                tool_kwargs["crawler_factory"] = injected_crawler
            if self._search_factory is not None:
                tool_kwargs["search_factory"] = self._search_factory
            injected_validator = getattr(self, "_url_validator_injected", False)
            if injected_validator:
                tool_kwargs["url_validator"] = self._validate_url

            runner = ReActRunner(
                self.llm_client,
                # budget 要透给工具闭包：extract_facts 的逐页 LLM 调用是单任务
                # 最大的 token 开销，不接进 TaskBudget 的话 token/USD 两道守卫
                # 会按"只有 ReAct 轮"的账放行，系统性低估。
                tools=build_default_tools(
                    self.llm_client, workspace, budget=budget, **tool_kwargs
                ),
                workspace=workspace,
                budget=budget,
                # token_budget 与 TaskBudget 同源（M6 双口径），None 时 Runner
                # 落 config 默认——生产二者本就同值
                token_budget=self._token_budget,
                on_step=_on_step,
            )
            result = await runner.run(user_query, target_urls=target_urls)

            state["cost_report"] = result.cost_report

            # workspace 的 facts 是 {url: [条目,...]}，拼平成 EvidenceStore 需要的列表
            facts = [
                item
                for items in (result.workspace.get("facts") or {}).values()
                for item in (items or [])
            ]

            answer = result.answer
            if answer:
                state["report_final"] = answer
                if facts:
                    store = EvidenceStore.from_extracted_data(facts)
                    citation_result = await asyncio.to_thread(verify_report, answer, store)
                    state["citation_report"] = {
                        k: v for k, v in citation_result.items() if k != "violations"
                    }
                    state["citation_violations"] = citation_result["violations"]
                    state["citation_trusted"] = citation_result["passed"]
                    if not citation_result["passed"]:
                        # 与 legacy 同一原则：未过校验的报告不静默交付，顶部挂低可信标记
                        state["report_final"] = self._mark_low_trust(state["report_final"], citation_result)

                self._enforce_report_output(state)
                state["sync_results"]["local_file"] = self._save_report(state["report_final"], state.get("task_id"))
                state["report_id"] = Path(state["sync_results"]["local_file"]).stem
                self._persist_to_sqlite(state)
                state["termination_reason"] = f"react:{result.stop_reason}"
                # M6：预算耗尽但终答已产出——结论照常交付，终态标 partial 而非 completed，
                # 让前端/落库能区分"跑完"与"被预算截断"
                if result.stop_reason == "budget_exceeded":
                    state["status"] = "partial"
                    self._emit(
                        TaskEventType.PARTIAL.value, "completed", "partial",
                        f"预算耗尽，按已获得的证据收敛交付（stop_reason={result.stop_reason}）",
                        payload={
                            "status": state["status"],
                            "termination_reason": state["termination_reason"],
                            "stop_reason": result.stop_reason,
                            "cost_report": state["cost_report"],
                        },
                    )
                else:
                    state["status"] = "completed"
                    self._emit("completed", "completed", "completed", "全部完成")
                self._update_task_status_in_sqlite(state)
            else:
                state["status"] = "failed"
                state["termination_reason"] = f"react:{result.stop_reason}"
                self._update_task_status_in_sqlite(state)
                state["report_final"] = (
                    f"❌ ReAct 调研未产出终答（stop_reason={result.stop_reason}，"
                    f"共执行 {len(result.steps)} 轮），可查看 agent_step 事件了解已完成的工具调用。"
                )
                self._emit(
                    "error", "agent", "failed",
                    f"ReAct 未产出终答：{result.stop_reason}",
                    payload={"stop_reason": result.stop_reason, "steps": len(result.steps)},
                )
        except Exception as e:
            logger.error(f"ReAct 工作流执行失败: {e}", exc_info=True)
            state["status"] = "failed"
            state["termination_reason"] = f"执行失败: {str(e)}"
            state["errors"].append({
                "timestamp": datetime.now(),
                "stage": "workflow",
                "error_type": type(e).__name__,
                "message": str(e),
                "retry_attempts": 0
            })
            self._emit(
                "error", "workflow", "failed",
                f"执行失败: {str(e)}",
                payload={"stage": "workflow", "error_type": type(e).__name__}
            )
            self._update_task_status_in_sqlite(state)

        # react 路径不走入口抓取，injection_findings 天然为空；统一记录计数保持口径一致
        state["injection_findings_count"] = len(state.get("injection_findings") or [])
        return state

    def _filter_search_urls(self, urls: List[str]) -> List[str]:
        """Keep only safe URLs from search results before the crawler sees them."""
        safe_urls = []
        for url in urls:
            if len(safe_urls) >= MAX_FETCH_URLS:
                break
            try:
                safe_urls.extend(self._validate_urls([url]))
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
        legacy_login_url = self._validate_url(login_url) if login_url else None

        for site in target_sites or []:
            url = self._validate_url(site["url"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            sites.append({
                "url": url,
                "auth_credentials": site.get("auth_credentials"),
                "login_url": self._validate_url(site["login_url"]) if site.get("login_url") else None,
            })

        for url in self._validate_urls(target_urls or []):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            sites.append({
                "url": url,
                "auth_credentials": auth_credentials,
                "login_url": legacy_login_url,
            })

        return sites

    def _initialize_state(self, user_query: str, target_urls: List[str], task_id: Optional[str] = None, user_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "task_id": task_id or str(uuid4()),
            "user_id": user_id,
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
            "source_coverage": None,
            "evidence_count": 0,
            "numeric_evidence_count": 0,
            "quality_score": None,
            "quality_grade": None,
            "missing_dimensions": [],
            "competitors": [],
            "comparison_matrix": None,
            "comparison_passed": None,
            "report_draft": None,
            "report_final": None,
            "errors": [],
            "injection_findings": [],
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
        """并行抓取多个竞品页面，限制并发数（动态调整）"""
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

        controller = get_concurrency_controller()

        async def _adjustment_loop():
            """后台定期调整并发数"""
            while True:
                await asyncio.sleep(5.0)
                await controller.check_and_adjust()

        adjustment_task = asyncio.create_task(_adjustment_loop())

        # 整批共享一个爬虫实例：连接池与会话复用，避免每站各开一套开销。
        # 刻意**惰性**创建而不是这里立即 new：单测常把 _fetch_single_competitor
        # 整个替身掉（不触网），那种调用方不应为此启动真实浏览器。首个真正
        # 抓取的站点经 _acquire_batch_crawler 创建一次，其余站点复用，
        # 批次结束由 batch_stack 统一关闭。
        async with contextlib.AsyncExitStack() as batch_stack:
            self._batch_crawler_stack = batch_stack
            self._batch_crawler = None
            self._batch_crawler_lock = asyncio.Lock()

            async def _limited_fetch(site: Dict[str, Any]) -> Dict[str, Any]:
                semaphore = controller.get_semaphore()
                async with semaphore:
                    # Rate limiting per site
                    await asyncio.sleep(CRAWL_REQUEST_INTERVAL)
                    return await self._fetch_single_competitor(
                        site["url"],
                        site.get("auth_credentials"),
                        site.get("login_url"),
                        custom_selectors,
                    )

            try:
                tasks = [_limited_fetch(site) for site in site_configs]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                # 解除批次上下文引用（爬虫本体由 batch_stack 退出时关闭），
                # 避免后续独立调用的单站抓取误用已关闭的实例
                self._batch_crawler_stack = None
                self._batch_crawler = None

        adjustment_task.cancel()
        try:
            await adjustment_task
        except asyncio.CancelledError:
            pass

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

    async def _acquire_batch_crawler(self) -> Optional[WebCrawler]:
        """惰性获取整批共享的爬虫实例（_fetch_all_competitors 建立的批次上下文）。

        不在批次内调用时返回 None，调用方退回就地新开即关。加锁防并发站点
        同时惰性创建出多个实例——那会退化回"每站一个爬虫"。
        """
        stack = getattr(self, "_batch_crawler_stack", None)
        if stack is None:
            return None
        async with self._batch_crawler_lock:
            if self._batch_crawler is None:
                self._batch_crawler = await stack.enter_async_context(self.crawler_factory())
            return self._batch_crawler

    async def _fetch_single_competitor(
        self,
        url: str,
        auth_credentials: Optional[Dict[str, str]] = None,
        login_url: Optional[str] = None,
        custom_selectors: Optional[Dict[str, List[str]]] = None,
        crawler: Optional[WebCrawler] = None,
        max_pages: Optional[int] = None,
        page_tiers: Optional[tuple] = None,
    ) -> Dict[str, Any]:
        """抓取单个竞品页面并提取信息。

        爬虫来源优先级：显式传入 crawler > 批次共享实例（_fetch_all_competitors
        建立，惰性创建整批复用）> 就地新开一个、用完即关（独立调用场景）。

        max_pages：可选。透传给 `fetch_site_profile` 的站内页数上限。
        补抓路径用它把每站抓取量限制在判定层分到的额度（per_candidate_pages）内；
        None 时沿用爬虫默认（整站 profile）。

        page_tiers：可选。透传给 `fetch_site_profile` 的站内发现排序（priority_tiers）。
        维度补抓用它让 discover 优先命中缺口维度对应的页面（见 dimension_refetch）。
        """
        self._emit("crawl_progress", "crawl", "running", f"正在抓取 {url}", payload={"url": url, "name": "unknown"})
        try:
            async with contextlib.AsyncExitStack() as stack:
                active_crawler = crawler
                if active_crawler is None:
                    active_crawler = await self._acquire_batch_crawler()
                if active_crawler is None:
                    active_crawler = await stack.enter_async_context(self.crawler_factory())
                # max_pages 只在补抓显式给定时透传；None 时不传，让爬虫走默认值
                # （FixtureSiteSource 的 max(1, max_pages) 对 None 会炸）
                profile_kwargs: Dict[str, Any] = dict(
                    auth_credentials=auth_credentials,
                    login_url=login_url,
                    custom_selectors=custom_selectors,
                )
                if max_pages is not None:
                    profile_kwargs["max_pages"] = max_pages
                if page_tiers is not None:
                    profile_kwargs["priority_tiers"] = page_tiers
                profile = await active_crawler.fetch_site_profile(url, **profile_kwargs)

                # 信任边界：抓到的正文是攻击者可控内容，必须在进入任何提示词之前清洗。
                # 探针取自**清洗前**原文，存进本次 run 的 state，留给报告出口做独立回声校验。
                raw_content = profile["content"]
                self._state().setdefault("injection_canaries", []).extend(
                    collect_canaries(raw_content)
                )
                cleaned_content, page_findings = sanitize(raw_content)
                self._record_injection_findings(url, page_findings)

                if should_drop_page(scan(raw_content)):
                    logger.warning(f"页面注入嫌疑过高，已整页丢弃: {url}")
                    self._emit(
                        "crawl_progress", "crawl", "running",
                        f"检测到注入攻击，已跳过 {url}",
                        payload={
                            "url": url,
                            "status": "blocked",
                            "recoverable": True,
                            "reason": "page_compromised",
                        },
                    )
                    return {
                        "name": infer_competitor_name(url),
                        "url": url,
                        "extracted_data": [],
                        "status": "blocked",
                        "error_message": "页面包含多条注入指令，已整页丢弃",
                        "injection_findings": len(page_findings),
                    }

                extracted = await self.coordinator.extract_key_info(cleaned_content, url)

            name = infer_competitor_name(url, extracted)
            page_count = len(profile.get("pages", []))
            self._emit("crawl_progress", "crawl", "running", f"抓取成功 {url}，覆盖 {page_count} 个站内页", payload={"url": url, "name": name, "status": "success", "page_count": page_count})
            page_summaries = [
                {
                    "url": page.get("url"),
                    "content_length": len(page.get("content", "")),
                }
                for page in profile.get("pages", [])
            ]

            return {
                "name": name,
                "url": url,
                "extracted_data": extracted,
                "raw_pages": page_summaries,
                "discovered_urls": profile.get("discovered_urls", []),
                "status": "success",
                "error_message": None
            }
        except Exception as e:
            logger.warning(f"抓取竞品失败 {url}: {e}")
            self._emit(
                "error", "crawl", "failed",
                f"抓取失败 {url}",
                payload={"url": url, "message": str(e), "recoverable": True}
            )
            return {
                "name": infer_competitor_name(url),
                "url": url,
                "extracted_data": [],
                "status": "failed",
                "error_message": str(e)
            }

    def _merge_refetched_into_competitors(
        self, state: Dict[str, Any], competitor_url: str, items: List[Dict[str, Any]]
    ) -> None:
        """把补抓到的数据回填到对应竞品。

        补抓的产出只进了 state["extracted_data"]（报告正文读它），没进
        state["competitors"][i]["extracted_data"]（评分器按竞品统计维度读它）——
        两边不一致，附录自评表就会说「定价缺失」而正文里正写着定价。
        """
        if not items:
            return
        target = str(competitor_url or "").rstrip("/")
        for comp in state.get("competitors", []):
            if str(comp.get("url") or "").rstrip("/") == target:
                comp.setdefault("extracted_data", []).extend(items)
                return

    def _refresh_quality_report(self, state: Dict[str, Any]):
        """按当前竞品数据重算数据完整度（补抓后必须重算）。

        附录自评表必须与报告正文看到的数据一致，否则报告会出现
        「正文有定价、附录说定价缺失」的自相矛盾——下游 judge 只能照着错账扣分。
        """
        quality_report = QualityScorer(state.get("competitors", [])).evaluate()
        state["quality_score"] = quality_report.overall_score
        state["quality_grade"] = quality_report.grade
        state["missing_dimensions"] = quality_report.missing_dimensions
        return quality_report

    async def _maybe_refetch_for_gaps(
        self,
        state: Dict[str, Any],
        evidence_store: EvidenceStore,
    ) -> Optional[Any]:
        """引用校验仍不过时，按判定层决定是否做一次定向补抓。

        闭环顺序固定为「补抓 → 带新证据重生成 → 第三次校验」（见调用方注释）：
        只重建证据库不重生成，报告里 [e:id] 仍指向旧证据，第三次校验必然不过——
        补抓的产出必须进入报告模型可见的证据表才有意义。

        补抓复用 `_fetch_single_competitor`：整站 profile（含站内发现）、注入防护、
        抽取全部走与初次抓取相同的链路，零新网络代码。每站页数被限制在判定层
        分到的额度（per_candidate_pages）内——这是补抓的成本刹车（单轮、不递归）。

        返回判定层决策；`eligible=False` 时返回 None，调用方照旧走低可信交付。
        """
        from src.agent.gap_refetch import (
            number_evidence_counts,
            plan_gap_refetch,
            refetchable_gaps,
            site_of,
        )

        violations = state.get("citation_violations") or []
        target_sites = [
            {"url": s.get("url", ""), "name": s.get("name", "")}
            for s in (state.get("target_sites") or [])
            if s.get("url")
        ]
        decision = plan_gap_refetch(
            violations, evidence_store, target_sites, max_pages=GAP_REFETCH_MAX_PAGES,
        )
        state["gap_refetch_decision"] = decision.to_dict()

        if not decision.eligible:
            self._emit(
                "gap_refetch", "verify", "skipped",
                f"不触发补抓（{decision.reason}）",
                payload=decision.to_dict(),
            )
            return None

        self._emit(
            "gap_refetch", "verify", "running",
            f"检测到 {len(decision.candidates)} 个竞品存在未解决的数值缺口，开始定向补抓",
            payload=decision.to_dict(),
        )

        new_evidence: List[Dict[str, Any]] = []
        refetched: List[Dict[str, Any]] = []
        async with contextlib.AsyncExitStack() as stack:
            crawler = await stack.enter_async_context(self.crawler_factory())
            for cand in decision.candidates:
                site_cfg = next(
                    (
                        s for s in (state.get("target_sites") or [])
                        if site_of(str(s.get("url") or "")) == cand.competitor_url
                    ),
                    None,
                )
                try:
                    comp = await self._fetch_single_competitor(
                        site_cfg.get("url") if site_cfg else cand.competitor_url,
                        auth_credentials=(site_cfg or {}).get("auth_credentials"),
                        login_url=(site_cfg or {}).get("login_url"),
                        custom_selectors=state.get("custom_selectors"),
                        max_pages=cand.per_candidate_pages,
                        crawler=crawler,
                    )
                except Exception as e:  # noqa: BLE001 —— 单站补抓失败不拖垮整批
                    logger.warning(f"补抓失败 {cand.competitor_url}: {e}")
                    refetched.append({
                        "url": cand.competitor_url, "status": "failed", "error": str(e),
                    })
                    continue
                refetched.append({
                    "url": cand.competitor_url,
                    "status": comp.get("status"),
                    "items": len(comp.get("extracted_data") or []),
                })
                new_evidence.extend(comp.get("extracted_data") or [])
                self._merge_refetched_into_competitors(
                    state, cand.competitor_url, comp.get("extracted_data") or [])

        state.setdefault("extracted_data", [])
        before = len(state["extracted_data"])
        state["extracted_data"].extend(new_evidence)
        state["refetched_evidence"] = new_evidence

        # 「可补抓的缺口」计数：**只做观测，不做闸门**。
        # 一度想用它拦住重生成，实测被现成夹具证伪——test_gap_refetch_flow 里
        # 那条「第三版把 99 改成 79」的闭环，缺口数字 99 从未出现在任何证据里，
        # 模型是靠重生成时拿到新证据表、自己把数字换成有据可引的那个。
        # 也就是说「站点上没有这个数 → 重生成也修不好」是错的，重生成恰恰能修。
        # 在拿到真实修复率数据之前，没有任何证据支持任何抑制规则，所以只记数。
        evidence_texts = [str(it.get("value") or "") for it in state["extracted_data"]]
        refetchable = refetchable_gaps(violations, evidence_texts)
        refetchable_count = sum(len(v) for v in refetchable.values())
        # 出现条数分布：只记 refetchable 的缺口数字（决策对象），不是全量分布。
        # 这是「到处都是 vs 压根不存在」阈值决策的数据采集——见 gap_refetch
        # 里 refetchable_gaps 的 shortcut 注释，攒够真实运行样本后按分布定阈值。
        gap_counts = number_evidence_counts(evidence_texts)
        evidence_spread = {
            token: gap_counts.get(token, 0)
            for tokens in refetchable.values()
            for token in tokens
        }

        state["gap_refetch_result"] = {
            "candidates": len(decision.candidates),
            "refetched": refetched,
            "new_items": len(new_evidence),
            "evidence_before": before,
            "evidence_after": len(state["extracted_data"]),
            "refetchable_gap_count": refetchable_count,
            "refetchable_gap_evidence_spread": evidence_spread,
        }

        self._emit(
            "gap_refetch", "verify", "completed",
            f"补抓完成：{len(new_evidence)} 条新证据（累计 {len(state['extracted_data'])} 条）",
            payload={
                "candidates": len(decision.candidates),
                "new_items": len(new_evidence),
                "evidence_after": len(state["extracted_data"]),
                "refetchable_gap_count": refetchable_count,
                # 事件进 Redis 持久化——真跑的分布样本从这里可查（evals 走
                # state 口径，两侧同源 number_evidence_counts）
                "refetchable_gap_evidence_spread": evidence_spread,
            },
        )
        return decision

    async def _maybe_refetch_dimensions(
        self,
        state: Dict[str, Any],
        evidence_store: EvidenceStore,
        remaining_budget: Optional[int] = None,
    ) -> Optional[Any]:
        """核心维度缺口 → 定向补抓（报告**没写**的维度，判定层看不到）。

        与 `_maybe_refetch_for_gaps`（数值缺口）并列：引用校验通过但核心维度
        missing/unconfirmed 时，报告没有该维度的断言。补抓用维度意图
        （DIMENSION_PAGE_TIERS，注入 discover 的 priority_tiers）选页，新证据
        进 evidence_block 重生成，让报告**新增**该维度断言。

        闭环顺序同数值补抓：「补抓 → 带新证据重生成 → 校验」（后两步在调用方）。
        预算：与数值补抓**共享** GAP_REFETCH_MAX_PAGES（拍板口径），`remaining_budget`
        是数值补抓扣完后的剩余；候选站数 = remaining//per_site_pages（每站 2 页
        = 首页 + 1 个维度意图页，见 plan_dimension_refetch 注释——1 页会只剩首页、
        选页意图失效）。剩余不足 2 页时**不触发**并记录 budget_insufficient，
        强行触发会退化回 per_site=1 的坑。单轮不递归。
        """
        from src.agent.dimension_refetch import (
            REASON_BUDGET_INSUFFICIENT,
            plan_dimension_refetch,
        )

        sites = [s for s in (state.get("target_sites") or []) if s.get("url")]
        budget = GAP_REFETCH_MAX_PAGES if remaining_budget is None else remaining_budget
        if budget < 2:
            state["dimension_refetch_decision"] = {
                "eligible": False,
                "reason": REASON_BUDGET_INSUFFICIENT,
                "candidates": [],
                "skipped": [],
            }
            self._emit(
                "gap_refetch", "verify", "skipped",
                f"维度补抓预算不足（剩余 {budget} 页 < 每站下限 2），跳过",
                payload={"reason": REASON_BUDGET_INSUFFICIENT, "remaining_budget": budget},
            )
            return None

        decision = plan_dimension_refetch(
            evidence_store, sites,
            max_pages=budget // 2, per_site_pages=2,
        )
        state["dimension_refetch_decision"] = decision.to_dict()

        if not decision.eligible:
            return None

        dims_needed = sorted({d for c in decision.candidates for d in c.dims})
        self._emit(
            "gap_refetch", "verify", "running",
            f"检测到 {len(decision.candidates)} 个竞品存在核心维度缺口"
            f"（{'、'.join(dims_needed)}），开始定向补抓",
            payload=decision.to_dict(),
        )

        new_evidence: List[Dict[str, Any]] = []
        refetched: List[Dict[str, Any]] = []
        async with contextlib.AsyncExitStack() as stack:
            crawler = await stack.enter_async_context(self.crawler_factory())
            for cand in decision.candidates:
                site_cfg = next(
                    (s for s in sites if str(s.get("url") or "") == cand.competitor_url),
                    None,
                )
                try:
                    comp = await self._fetch_single_competitor(
                        site_cfg.get("url") if site_cfg else cand.competitor_url,
                        auth_credentials=(site_cfg or {}).get("auth_credentials"),
                        login_url=(site_cfg or {}).get("login_url"),
                        custom_selectors=state.get("custom_selectors"),
                        max_pages=cand.pages,
                        page_tiers=cand.page_tiers,
                        crawler=crawler,
                    )
                except Exception as e:  # noqa: BLE001 —— 单站补抓失败不拖垮整批
                    logger.warning(f"维度补抓失败 {cand.competitor_url}: {e}")
                    refetched.append({
                        "url": cand.competitor_url, "status": "failed", "error": str(e),
                    })
                    continue
                refetched.append({
                    "url": cand.competitor_url,
                    "status": comp.get("status"),
                    "items": len(comp.get("extracted_data") or []),
                })
                new_evidence.extend(comp.get("extracted_data") or [])
                self._merge_refetched_into_competitors(
                    state, cand.competitor_url, comp.get("extracted_data") or [])

        state.setdefault("extracted_data", [])
        before = len(state["extracted_data"])
        state["extracted_data"].extend(new_evidence)
        state["refetched_evidence"] = (state.get("refetched_evidence") or []) + new_evidence
        state["dimension_refetch_result"] = {
            "candidates": len(decision.candidates),
            "dims": dims_needed,
            "refetched": refetched,
            "new_items": len(new_evidence),
            "evidence_before": before,
            "evidence_after": len(state["extracted_data"]),
        }

        self._emit(
            "gap_refetch", "verify", "completed",
            f"维度补抓完成：{len(new_evidence)} 条新证据（累计 {len(state['extracted_data'])} 条）",
            payload={
                "candidates": len(decision.candidates),
                "dims": dims_needed,
                "new_items": len(new_evidence),
                "evidence_after": len(state["extracted_data"]),
            },
        )
        return decision

    def _save_report(self, content: str, task_id: Optional[str] = None) -> str:
        """保存报告到本地文件"""
        from src.config.settings import OUTPUT_DIR
        import os

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 附上 task_id 前 8 位：秒级时间戳在多用户并发下会撞车，导致互相覆盖。
        # task_id 是 uuid4 派生的，不含用户信息，不会成为枚举入口。
        suffix = f"_{task_id[:8]}" if task_id else ""
        filename = f"research_report_{timestamp}{suffix}.md"
        filepath = os.path.join(OUTPUT_DIR, filename)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath

    def _persist_to_sqlite(self, state: Dict[str, Any]):
        """Persist report metadata to SQLite for indexing."""
        try:
            from src.storage import get_report_db
            import hashlib
            db = get_report_db()

            user_query = state.get("user_query")
            user_id = state.get("user_id")
            # 版本链键必须按用户隔离。只用 query_hash 会让用户 A 跑相同问题时，
            # 把报告接到他人（旧版本）之后 —— 等于通过版本链跨用户泄露内容。
            # 用 NUL 分隔，避免拼接产生歧义；无主数据统一落到 anon 命名空间。
            scope = user_id or "\x00anon"
            query_hash = (
                hashlib.sha256(f"{scope}\x00{user_query}".encode()).hexdigest()[:16]
                if user_query
                else None
            )

            # Check if there's an existing report for this query
            existing_report_id = None
            if query_hash:
                latest = db.get_latest_version(query_hash)
                if latest:
                    existing_report_id = latest["id"]

            report_id = state.get("report_id")
            parent_version_id = existing_report_id if existing_report_id and existing_report_id != report_id else None

            if parent_version_id:
                # This is a new version - use save_report_version
                db.save_report_version(
                    report_id=report_id,
                    task_id=state.get("task_id"),
                    user_query=user_query,
                    report_content=state.get("report_final"),
                    quality_score=state.get("quality_score"),
                    quality_grade=state.get("quality_grade"),
                    version_number=1,  # Will be auto-incremented by link_report_version
                    parent_version_id=parent_version_id,
                    user_id=user_id,
                    metadata={
                        "competitors": [
                            {"name": c.get("name"), "url": c.get("url")}
                            for c in state.get("competitors", [])
                            if c.get("status") == "success"
                        ],
                        "missing_dimensions": state.get("missing_dimensions", []),
                        "verification_score": state.get("verification_score"),
                    },
                )
                db.link_report_version(query_hash, report_id, parent_version_id)
            else:
                # First version - save normally then link
                db.save_report(
                    report_id=report_id,
                    task_id=state.get("task_id"),
                    user_query=user_query,
                    report_content=state.get("report_final"),
                    quality_score=state.get("quality_score"),
                    quality_grade=state.get("quality_grade"),
                    user_id=user_id,
                    metadata={
                        "competitors": [
                            {"name": c.get("name"), "url": c.get("url")}
                            for c in state.get("competitors", [])
                            if c.get("status") == "success"
                        ],
                        "missing_dimensions": state.get("missing_dimensions", []),
                        "verification_score": state.get("verification_score"),
                    },
                )
                if query_hash:
                    db.link_report_version(query_hash, report_id, None)

            db.save_task(
                task_id=state.get("task_id"),
                user_query=user_query,
                status=state.get("status"),
                report_id=report_id,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning(f"Failed to persist to SQLite: {e}")

    def _update_task_status_in_sqlite(self, state: Dict[str, Any]):
        """把**终态**补写进 SQLite 的 tasks 表。

        `_persist_to_sqlite` 写的是 `state.get("status")`，而它必须在报告落盘后
        立刻调用（PRD 生成、文档同步可能再抛异常，那时报告已经写完却还没进库）。
        于是那一刻 status 仍是 `reacting` / `writing` 之类的中间态——库里的行
        会永远停在中间态，因为 tasks 表此前只写不读，缺陷一直没暴露。

        行不存在时（未产出终答的 failed 路径压根到不了 `_persist_to_sqlite`）
        退回 save_task 建行，否则 UPDATE 打在空行上是 no-op，失败任务仍然留不下
        任何痕迹。异常同样吞成 warning：状态补写失败不该让已交付的任务反过来失败。
        """
        task_id = state.get("task_id")
        status = state.get("status")
        if not task_id or not status:
            return
        try:
            from src.storage import get_report_db
            db = get_report_db()
            updated = db.update_task_status(
                task_id, status, report_id=state.get("report_id"),
            )
            if not updated:
                db.save_task(
                    task_id=task_id,
                    user_query=state.get("user_query"),
                    status=status,
                    report_id=state.get("report_id"),
                    user_id=state.get("user_id"),
                )
        except Exception as e:
            logger.warning(f"Failed to update task status in SQLite: {e}")

    async def _sync_documents(self, state: Dict[str, Any], sync_targets: List[str]) -> Dict[str, Any]:
        """同步到外部文档平台"""
        title = f"竞品调研报告-{state.get('user_query', '未命名')[:20]}"

        for target in sync_targets:
            try:
                if target == "feishu":
                    result = await asyncio.to_thread(
                        doc_sync_manager.sync_to_feishu, state["report_final"], title
                    )
                    if "error" in result:
                        raise Exception(result["error"])
                    state["sync_results"]["feishu"] = result.get("url", result.get("document_id", ""))
                elif target == "tencent":
                    result = await asyncio.to_thread(
                        doc_sync_manager.sync_to_tencent, state["report_final"], title
                    )
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
