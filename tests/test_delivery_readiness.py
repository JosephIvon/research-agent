"""交付级整改（第一批）的守卫。

用户实测（4 家真实竞品）暴露的问题，这里逐条钉成回归：

1. 抽取调用必须有**自己的** token 预算——沿用客户端默认 4096 时，推理模型（M3）
   会把预算先花在思考上，正文一个字不剩（实测 Empty response ×2）。
2. 抽取失败要重试一次；兜底条目必须带失败标记——否则整页原文里的"定价"两个字
   就能让一家没抽出任何数据的竞品在维度覆盖表里显示 ✅。
3. 抓到了却抽不出数据的竞品必须扣分——4 家里 2 家没数据时不得再打 10/10 A 级。
4. 任务失败时不得留着报告生成前算出的分数（实测 failed 仍显示 10/10 A 级）。
5. 竞品名要真名，域名只是最后兜底（实测 Chuanweidp / Meicai / 123 / Cloud）。
"""
import asyncio
import json
from pathlib import Path

import pytest

from src.tools.comparison_tool import infer_competitor_name
from src.tools.quality_scorer import QualityScorer
from src.workflow.mvp_workflow import EXTRACTION_MAX_TOKENS, CoordinatorAgent

REPO_ROOT = Path(__file__).resolve().parents[1]


class _RecordingLLM:
    """按脚本回放响应，并记录每次调用收到的参数与提示词。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def achat_completion(self, prompt, **kwargs):
        self.calls.append({"method": "achat_completion", "messages": prompt, **kwargs})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]

    def stream_completion(self, prompt, **kwargs):
        """报告生成走流式；同一次调用必须能被认出来是走的哪条路。"""
        self.calls.append({"method": "stream_completion", "messages": prompt, **kwargs})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


def _coordinator(llm) -> CoordinatorAgent:
    agent = CoordinatorAgent.__new__(CoordinatorAgent)
    agent.llm_client = llm
    agent.search_tool = None
    return agent


# --------------------------------------------------------------------------
# 1. 抽取：自己的预算 + 重试 + 失败标记
# --------------------------------------------------------------------------

def test_extraction_uses_its_own_token_budget():
    """抽取必须带自己的 max_tokens——推理块挤占预算是实测过的失败模式。"""
    llm = _RecordingLLM([json.dumps([{"key": "pricing", "value": "49 美元"}])])

    items = asyncio.run(
        _coordinator(llm).extract_key_info("页面内容：标准版 49 美元", "https://acme.test")
    )

    assert llm.calls, "抽取根本没有调用 LLM"
    assert llm.calls[0]["max_tokens"] == EXTRACTION_MAX_TOKENS
    assert llm.calls[0]["max_tokens"] > 4096, "抽取预算必须大于客户端默认的 4096"
    assert items[0]["key"] == "pricing"


def test_extraction_retries_once_when_the_first_response_is_empty():
    """空响应（思考吃光预算）要重试一次，而不是直接退化成无结构原文。"""
    llm = _RecordingLLM(["", json.dumps([{"key": "pricing", "value": "49 美元"}])])

    items = asyncio.run(
        _coordinator(llm).extract_key_info("页面内容：标准版 49 美元", "https://acme.test")
    )

    assert len(llm.calls) == 2, "第一次空响应后必须重试一次"
    assert items[0]["key"] == "pricing", items
    assert not items[0].get("extraction_failed"), "重试成功了就不该留失败标记"
    # 重试时必须把要求收紧到"只吐 JSON"
    retry_text = " ".join(str(m.get("content", "")) for m in llm.calls[1]["messages"])
    assert "JSON" in retry_text, "重试提示必须要求只输出 JSON"


def test_extraction_fallback_is_marked_as_failed():
    """两次都失败时，兜底条目必须带失败标记——它不是结构化事实。"""
    llm = _RecordingLLM(["", ""])

    items = asyncio.run(
        _coordinator(llm).extract_key_info("页面里写着定价 49 美元", "https://acme.test")
    )

    assert len(llm.calls) == 2
    assert items[0].get("extraction_failed") is True, "兜底条目必须自报失败"
    assert items[0]["key"] == "content_summary"
    assert "49" in items[0]["value"], "兜底仍要保住页面原文，否则报告无据可依"


# --------------------------------------------------------------------------
# 2. 评分：无数据的竞品必须扣分
# --------------------------------------------------------------------------

_DIM_KEYS = [
    "pricing", "features", "models", "target_users",
    "integrations", "limitations", "reviews",
]


def _good_competitor(name: str = "Acme"):
    """数据齐全的竞品：每个维度 3 条。

    条数直接决定维度得分（1 条 3 分 / 2 条 5 分 / 3–5 条 7 分 / 再多每条 +0.5），
    所以这里必须给够——否则"半份空壳"的对照组即使去掉折扣也够不到 A 级，
    守卫会退化成"怎么改都不红"的假阳性。
    """
    items = [
        {"key": key, "value": f"{key} 条目 {n} 的说明文字"}
        for key in _DIM_KEYS
        for n in (1, 2, 3)
    ]
    return {
        "name": name,
        "status": "success",
        "extracted_data": items,
    }


def _broken_competitor(name: str = "Broken"):
    """抓到了但抽取失败的竞品：兜底原文里恰好含"定价"两个字。"""
    return {
        "name": name,
        "status": "success",
        "extracted_data": [
            {
                "key": "content_summary",
                "value": "本站定价 49 美元，功能丰富，支持多种集成……",
                "source_url": "https://broken.test",
                "extraction_failed": True,
            },
        ],
    }


def test_half_broken_competitors_cannot_score_level_a():
    """4 家里 2 家抽不出数据时，不得再打 10/10 A 级。"""
    competitors = [
        _good_competitor("A"), _broken_competitor("B"),
        _good_competitor("C"), _broken_competitor("D"),
    ]

    report = QualityScorer(competitors).evaluate()

    assert report.overall_score < 10.0, f"半份空壳却拿了满分：{report.overall_score}"
    assert report.grade != "A", f"半份空壳却拿了 A 级：{report.grade}"


def test_fallback_text_is_not_counted_as_dimension_data():
    """兜底原文里的"定价"两个字不算维度数据。"""
    report = QualityScorer([_broken_competitor("B")]).evaluate()

    assert len(report.missing_dimensions) == len(QualityScorer.DIMENSIONS), \
        report.missing_dimensions
    assert report.overall_score < 4.0, report.overall_score


def test_all_good_competitors_are_not_penalised():
    """负向对照：数据齐全时不得被"有数据竞品占比"打折。"""
    competitors = [_good_competitor("A"), _good_competitor("B"), _good_competitor("C")]
    scorer = QualityScorer(competitors)

    report = scorer.evaluate()
    unscored = scorer._calculate_overall_score(report.dimensions, 3)

    assert report.overall_score == pytest.approx(unscored), \
        f"有数据竞品占比把分数压下去了：{report.overall_score} < {unscored}"
    assert report.overall_score >= 4.0, report.overall_score


# --------------------------------------------------------------------------
# 3. 失败路径不得留下高分
# --------------------------------------------------------------------------

def test_failed_run_does_not_keep_a_high_score(tmp_path, monkeypatch):
    """报告生成失败时，任务不得挂着报告生成前算出的分数（实测 10/10 A 级）。"""
    from tests.test_agent_mode_wiring import EXTRACT_ITEMS, _wire_offline
    from src.llm import ScriptedLLMClient

    monkeypatch.setenv("AGENT_MODE", "legacy")
    llm = ScriptedLLMClient(script={
        "extract": [json.dumps(EXTRACT_ITEMS, ensure_ascii=False)],
        "report": [""],  # 空正文 → generate_report 显式抛错
    })
    _wire_offline(monkeypatch, tmp_path, llm)

    from src.workflow import mvp_workflow
    workflow = mvp_workflow.MVPWorkflow()
    state = asyncio.run(workflow.run(
        "Acme 定价调研",
        target_urls=["https://acme.example.com/pricing"],
        enable_search=False,
        deliverables={"report": True, "prd": False},
        task_id="failed-score-0001",
    ))

    assert state["status"] == "failed", state.get("termination_reason")
    assert state["quality_score"] is None, \
        f"失败任务仍挂着分数：{state['quality_score']}（{state['quality_grade']}）"
    assert state["quality_grade"] is None


# --------------------------------------------------------------------------
# 4. 竞品名：域名只是最后兜底
# --------------------------------------------------------------------------

def test_page_title_wins_over_the_domain():
    assert infer_competitor_name(
        "https://chuanweidp.example.com", [], "川为 - 餐饮供应链"
    ) == "川为"


def test_extracted_brand_name_wins_over_the_title():
    assert infer_competitor_name(
        "https://chuanweidp.example.com",
        [{"key": "name", "value": "川为供应链"}],
        "川为 - 餐饮供应链",
    ) == "川为供应链"


def test_domain_is_only_the_last_resort():
    assert infer_competitor_name("https://chuanweidp.example.com", [], None) == "Chuanweidp"


def test_cli_label_matches_the_metric_it_prints():
    """CLI 打印的"来源覆盖率"不是"可信度"——名字不能说谎。"""
    source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "可信度评分" not in source, "来源覆盖率被印成了可信度"
    assert "来源覆盖率" in source


# --------------------------------------------------------------------------
# 5. 超时默认值
# --------------------------------------------------------------------------

def test_llm_read_timeout_default_is_300s():
    """长报告在 120s 处被掐断（实测 APITimeoutError）——默认值必须留在 300s。"""
    from src.config.settings import LLM_READ_TIMEOUT

    assert LLM_READ_TIMEOUT == 300, LLM_READ_TIMEOUT


def test_config_manager_shares_the_same_timeout_default():
    """一个数字只允许有一处权威来源：配置管理器不得另写一份默认值。"""
    from src.config.config_manager import ConfigManager
    from src.config.settings import LLM_READ_TIMEOUT

    assert ConfigManager().get("LLM_READ_TIMEOUT") == LLM_READ_TIMEOUT
