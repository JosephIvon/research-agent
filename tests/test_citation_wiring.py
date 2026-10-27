"""引用校验闭环接入工作流的测试。

这里刻意**不跑完整 run()**：它依赖真实 LLM、爬虫、搜索与 SQLite。
接线正确性改用两条更强的方式保证：
1. 直接构造真实依赖的替身，调用被测的生产函数；
2. 用 `inspect.getsource` 断言守卫确实接在交付路径上（避免"方法写了但没人调"）。
"""
import asyncio
import inspect

import pytest

from src.agent.citations import EvidenceStore, verify_report
from src.workflow.mvp_workflow import CopywriterAgent, MVPWorkflow

EXTRACTED = [
    {
        "key": "pricing",
        "value": "标准版 $49/席位/月，企业版 $99/席位/月。",
        "source_url": "https://acme.test/pricing",
        "confidence": 0.9,
    },
]


class RecordingLLM:
    """按调用顺序返回脚本化响应，并记录每次的提示词。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def achat_completion(self, prompt, **kwargs):
        self.calls.append(prompt)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    def stream_completion(self, prompt, **kwargs):
        """报告生成走流式（见 mvp_workflow.generate_report），记录方式保持一致。"""
        self.calls.append(prompt)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def make_writer(responses):
    writer = CopywriterAgent.__new__(CopywriterAgent)
    writer.llm_client = RecordingLLM(responses)
    return writer


# --------------------------------------------------------------------------
# 提示词：喂证据表、要求 [e:id]
# --------------------------------------------------------------------------

def test_prompt_contains_compact_evidence_table():
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    ev = store.evidences[0].citation
    writer = make_writer([f"标准版 $49/席位/月 {ev}。\n"])

    asyncio.run(writer.generate_report("AI 客服对比", EXTRACTED, {"score": 8}))

    system, user = writer.llm_client.calls[0][0]["content"], writer.llm_client.calls[0][-1]["content"]
    assert ev in system + user, "提示词必须给出可引用的证据 id"
    assert "https://acme.test/pricing" in user
    assert "含数字" in system and "[e:" in system, "必须明确要求事实句带证据标记"
    assert "整句删除" in system, "必须禁止编造证据与保留无源断言"


def test_both_prompt_branches_get_the_evidence_table():
    """多竞品与单竞品两条分支都要喂证据表——只改一条等于漏一半场景。"""
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    ev = store.evidences[0].citation

    multi = make_writer([f"$49 {ev}。\n"])
    asyncio.run(multi.generate_report(
        "对比", EXTRACTED, {"score": 8},
        competitors=[
            {"name": "A", "url": "https://a.test", "status": "success", "extracted_data": EXTRACTED},
            {"name": "B", "url": "https://b.test", "status": "success", "extracted_data": EXTRACTED},
        ],
    ))
    assert ev in multi.llm_client.calls[0][-1]["content"]

    single = make_writer([f"$49 {ev}。\n"])
    asyncio.run(single.generate_report("对比", EXTRACTED, {"score": 8}))
    assert ev in single.llm_client.calls[0][-1]["content"]


def test_violation_feedback_is_injected_on_regeneration():
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    ev = store.evidences[0].citation
    writer = make_writer([f"$49 {ev}。\n"])

    asyncio.run(writer.generate_report(
        "对比", EXTRACTED, {"score": 8},
        evidence_store=store,
        violations_feedback="【上一版报告存在的问题，必须逐条修正】\n- [ungrounded_claim] 数值 7 找不到",
    ))

    assert "ungrounded_claim" in writer.llm_client.calls[0][-1]["content"]


def test_generated_report_passes_the_same_store_it_was_given():
    """端到端：喂什么证据表，就必须能用同一份证据校验通过。

    若 generate_report 内部自己重建 store，id 会对不上，这里会全量报 unknown_citation。
    """
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    ev = store.evidences[0].citation
    writer = make_writer([f"标准版 $49/席位/月 {ev}。\n"])

    report = asyncio.run(writer.generate_report("对比", EXTRACTED, {"score": 8}, evidence_store=store))

    result = verify_report(report, store)
    assert result["passed"] is True, result["violations"]


# --------------------------------------------------------------------------
# 低可信标记
# --------------------------------------------------------------------------

def _workflow_stub():
    workflow = object.__new__(MVPWorkflow)
    workflow.state = {"task_id": "cite-test"}
    return workflow


def test_low_trust_mark_lists_the_violations():
    workflow = _workflow_stub()
    report = "## 定价\n\n本产品定价 $9/席位/月。\n"

    marked = workflow._mark_low_trust(report, verify_report(report, EvidenceStore.from_extracted_data(EXTRACTED)))

    assert marked.startswith("> ⚠️ **低可信标记")
    assert "uncited_claim" in marked
    assert report in marked, "原报告正文必须保留，不能被标记覆盖"


def test_low_trust_mark_states_it_already_retried_once():
    workflow = _workflow_stub()
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    report = "本产品定价 $9/席位/月。\n"
    marked = workflow._mark_low_trust(report, verify_report(report, store))
    assert "重生成" in marked


def test_passing_report_is_not_marked():
    """通过校验的报告不得被挂上低可信标记。"""
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    ev = store.evidences[0].citation
    result = verify_report(f"标准版 $49/席位/月 {ev}。\n", store)
    assert result["passed"] is True

    workflow = _workflow_stub()
    marked = workflow._mark_low_trust("正常报告。\n", result)
    assert "低可信标记" not in marked


GAP_REPORT = (
    "**容量评估**：付费层限速策略公开页面未披露，建议向厂商索取书面确认，"
    "其中限速 60 次/分钟。\n"
)
ASSERTION_REPORT = "Acme 企业版定价 39 美元/席位/月。\n"


def test_low_trust_mark_separates_gap_acknowledgement_from_accusations():
    """真·无据断言和"已标注的信息缺口"必须分组。

    混在一起时，用户读到的是系统在指控一篇诚实标注了缺口的报告——
    真实 MiniMax 冒烟里被判 uncited 的正是缺口句。
    """
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    marked = _workflow_stub()._mark_low_trust(GAP_REPORT, verify_report(GAP_REPORT, store))

    assert "信息缺口" in marked
    assert "不需要" in marked or "无需修正" in marked
    assert "需要修正" in marked or "无需修正" in marked, "必须明确说清哪些不用改"
    assert GAP_REPORT in marked, "原文必须保留"


def test_low_trust_mark_still_lists_real_assertions_under_repair():
    workflow = _workflow_stub()
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    marked = workflow._mark_low_trust(ASSERTION_REPORT, verify_report(ASSERTION_REPORT, store))

    assert "需要修正" in marked
    assert "未标注证据" in marked
    assert "信息缺口" not in marked.split("---")[0], "纯断言场景不该出现缺口分组"


def test_grouping_never_turns_a_failing_report_into_a_passing_one():
    """底线：分组只改措辞。缺口句照样让报告不通过。"""
    store = EvidenceStore.from_extracted_data(EXTRACTED)
    gap_result = verify_report(GAP_REPORT, store)
    assert gap_result["passed"] is False
    assert gap_result["violation_count"] > 0

    marked = _workflow_stub()._mark_low_trust(GAP_REPORT, gap_result)
    assert "低可信标记" in marked, "缺口场景仍然必须挂标记"


# --------------------------------------------------------------------------
# 接线：守卫确实在交付路径上
# --------------------------------------------------------------------------

def test_citation_guard_is_wired_into_run():
    """校验 + 一次重生成 + 低可信标记必须都在 run() 里，不能是没人调用的方法。

    断言必须精确到**调用点**：只断言 `"verify_report" in source` 会被"把函数体换成硬编码
    假结果、但 import 还在"的变异蒙混过去——变异验证已经抓到过这种假绿。
    """
    source = inspect.getsource(MVPWorkflow.run)

    # 至少两次真实校验调用（初稿 + 重生成后是闭环基线）。缺口定向补抓
    # （gap refetch）在判定触发时会追加第三次校验（补抓 → 重生成 → 校验），
    # 所以不锁死次数；变异验证的强度来自"必须真的调用"，不是次数本身。
    assert source.count(
        'verify_report, state["report_final"], evidence_store'
    ) >= 2, "初稿与重生成后都必须各跑一次真实校验"

    assert "format_violations" in source, "重生成必须带违规清单，否则模型不知道改什么"
    assert "violations_feedback=feedback," in source, "必须把违规清单真的传进重生成提示词"
    assert "_mark_low_trust(state[\"report_final\"], citation_result)" in source, \
        "不通过时必须显式标记，不能静默交付"
    assert "citation_trusted" in source

    # 顺序：必须先校验后落盘
    assert source.index("verify_report") < source.index("_save_report(")
    assert source.index("violations_feedback=feedback,") < source.index("_save_report(")


def test_run_marks_low_trust_before_saving_to_disk():
    """低可信标记要落在写文件之前——否则磁盘上的报告不带标记。"""
    source = inspect.getsource(MVPWorkflow.run)
    assert "_mark_low_trust(state[\"report_final\"], citation_result)" in source
    assert source.index("_mark_low_trust(state[\"report_final\"], citation_result)") < source.index("_save_report(")


def test_citation_check_happens_after_injection_guard_is_kept():
    """注入闸与引用闸都要在交付前，不允许其中一道被摘掉。"""
    source = inspect.getsource(MVPWorkflow.run)
    assert "_enforce_report_output(state)" in source
    assert source.index("_enforce_report_output(state)") < source.index("_save_report(")


def test_quality_section_is_labelled_completeness_not_trustworthiness():
    """数据完整度章节不得再自称「质量评分」或「可信度」。

    它只回答"抓到多少数据"，回答不了"结论对不对"——叫可信度就是用数据量冒充事实正确。
    """
    from src.tools.quality_scorer import QualityScorer

    section = QualityScorer([]).generate_quality_section()

    assert "数据完整度" in section
    assert "不衡量结论正确性" in section, "必须当场声明能力边界"
    assert "质量评分" not in section
    assert "可信度评分" not in section


def test_extraction_gate_is_not_called_trustworthiness():
    """抽取门禁不得对外称作「可信度」。"""
    source = inspect.getsource(MVPWorkflow.run)

    assert "可信度评分" not in source, "旧措辞仍在把数据可追溯性说成可信度"
    assert "不代表结论正确性" in source
    assert "来源覆盖率" in source
    assert "可信度由引用校验负责" in source, "必须写明职责边界：可信度归引用校验管"