"""抽取质量门禁的测试：它必须拦得住原实现放行的东西。

背景：旧 `verify_consistency()` 对 LLM 自报 confidence 求平均作为门禁。实测行为——
    全部 confidence=0.8（空壳数据）→ 8.0 通过
    全部 confidence=0.5（最脏数据）→ 5.0 通过
    空列表                        → 0.0 不通过
即除了"一条都没有"什么都放行。本组测试钉死替代口径：**来源可追溯性**。
"""
import asyncio

import pytest

from src.workflow.mvp_workflow import CoordinatorAgent


@pytest.fixture
def coordinator():
    return CoordinatorAgent.__new__(CoordinatorAgent)


def assess(coordinator, data):
    return asyncio.run(coordinator.assess_extracted_data(data))


def item(**kw):
    base = {"key": "pricing", "value": "定价 $49/席位/月", "source_url": "https://a.test"}
    base.update(kw)
    return base


def many(n, **kw):
    return [item(key=f"k{i}", **kw) for i in range(n)]


# --------------------------------------------------------------------------
# 旧实现放行、现在必须拦住的情况
# --------------------------------------------------------------------------

def test_dirty_low_confidence_data_no_longer_passes_by_default(coordinator):
    """旧口径下 confidence 全 0.5 依然放行；新口径下条数不足必须拦住。"""
    data = many(2, confidence=0.5)  # 只有 2 条，低于最低证据条数 3

    result = assess(coordinator, data)

    assert result["passed"] is False
    assert result["evidence_count"] == 2


def test_high_self_confidence_does_not_buy_a_pass(coordinator):
    """模型把自己打满分也不能替代"有来源"和"条数够"。"""
    result = assess(coordinator, many(1, confidence=1.0))

    assert result["passed"] is False, "1 条数据 + 自评满分不构成通过理由"


def test_unsourced_evidence_is_rejected(coordinator):
    """没有 source_url 的事实无法被引用校验追溯，必须拦住。"""
    data = many(5, source_url="")

    result = assess(coordinator, data)

    assert result["passed"] is False
    assert result["source_coverage"] == 0.0
    assert any("缺少来源链接" in w for w in result["warnings"])


def test_partial_source_coverage_below_threshold_is_rejected(coordinator):
    data = [item(source_url="https://a.test")] * 6 + [item(source_url="")] * 2  # 6/8 = 0.75

    result = assess(coordinator, data)

    assert result["source_coverage"] == pytest.approx(0.75, abs=1e-3)
    assert result["passed"] is False


# --------------------------------------------------------------------------
# 正常数据必须能通过
# --------------------------------------------------------------------------

def test_healthy_data_passes(coordinator):
    result = assess(coordinator, many(5))

    assert result["passed"] is True
    assert result["source_coverage"] == 1.0
    assert result["evidence_count"] == 5
    assert result["numeric_evidence_count"] == 5
    assert result["warnings"] == []


def test_empty_data_fails_with_clear_reason(coordinator):
    result = assess(coordinator, [])

    assert result["passed"] is False
    assert result["score"] == 0.0
    assert "没有抽取到任何数据" in result["warnings"][0]


# --------------------------------------------------------------------------
# score 的语义
# --------------------------------------------------------------------------

def test_score_is_source_coverage_not_confidence(coordinator):
    """score 必须等于来源覆盖率×10 —— 它不再代表任何可信度。"""
    result = assess(coordinator, [item(source_url="https://a.test")] * 8 + [item(source_url="")] * 2)  # 8/10 = 0.8

    assert result["score"] == pytest.approx(8.0)
    assert result["source_coverage"] == pytest.approx(0.8)


def test_identical_confidences_produce_identical_results(coordinator):
    """门禁必须是确定性的：同一批数据永远同一结论。"""
    data = many(4, confidence=0.42)
    assert assess(coordinator, data) == assess(coordinator, data)


# --------------------------------------------------------------------------
# 模型自评只做警告，不参与门禁
# --------------------------------------------------------------------------

def test_low_model_confidence_warns_but_does_not_gate(coordinator):
    result = assess(coordinator, many(5, confidence=0.3))

    assert result["passed"] is True, "模型自评不得决定门禁"
    assert any("模型自评，非客观指标" in w for w in result["warnings"])


def test_warning_about_no_numeric_evidence(coordinator):
    result = assess(coordinator, many(4, value="支持企业级部署"))

    assert result["numeric_evidence_count"] == 0
    assert any("没有任何一条数据包含数值" in w for w in result["warnings"])


# --------------------------------------------------------------------------
# 边界
# --------------------------------------------------------------------------

def test_blank_source_url_counts_as_missing(coordinator):
    result = assess(coordinator, many(3, source_url="   "))
    assert result["source_coverage"] == 0.0
    assert result["passed"] is False


def test_non_dict_items_do_not_crash(coordinator):
    """畸形数据不该让门禁抛异常——抛异常会被上层当成执行失败，掩盖真实原因。"""
    result = assess(coordinator, [item(), "垃圾数据", None, 42])

    assert result["evidence_count"] == 4
    assert 0.0 <= result["source_coverage"] <= 1.0


def test_model_self_assessment_never_decides_the_gate(coordinator):
    """模型自评 confidence 只能在阈值上下**同时**不改变门禁结果。

    这是"循环论证"的核心防线：让被考核者自己打分，就得证明打分权重为零。
    """
    healthy = many(5)
    high = many(5, confidence=1.0)
    low = many(5, confidence=0.1)

    results = [assess(coordinator, d) for d in (healthy, high, low)]

    assert {r["passed"] for r in results} == {True}, "三种自评必须得到同一门禁结论"
    assert len({r["source_coverage"] for r in results}) == 1, "覆盖率与自评无关"
    assert len({r["evidence_count"] for r in results}) == 1
    # 只有 warnings 随自评变化——那是它的正当用途：提示而非判定
    assert results[1]["warnings"] == []
    assert any("模型自评" in w for w in results[2]["warnings"])


def test_verify_consistency_no_longer_exists(coordinator):
    """旧方法必须消失，不能与新方法并存形成两套口径。"""
    assert not hasattr(coordinator, "verify_consistency")
    assert hasattr(coordinator, "assess_extracted_data")