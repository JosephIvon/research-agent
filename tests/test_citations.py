"""引用校验闭环的测试。

关键设计：所有断言都打在**生产代码**上（`src/agent/citations.py`），
不在测试里重算一遍判定逻辑——那是"自测自"，校验器写错了测试也跟着绿。
"""
import pytest

from src.agent.citations import (
    CITATION_RE,
    DEFAULT_THRESHOLDS,
    EvidenceStore,
    extract_numbers,
    format_violations,
    iter_claims,
    make_evidence_id,
    verify_report,
)


@pytest.fixture
def store():
    """两条真实抓取到的证据。"""
    return EvidenceStore.from_extracted_data([
        {
            "key": "pricing",
            "value": "标准版定价 $49/席位/月，企业版 $99/席位/月。",
            "source_url": "https://acme.test/pricing",
        },
        {
            "key": "models",
            "value": "支持 GPT-4、Claude、Gemini 三类模型。",
            "source_url": "https://acme.test/models",
        },
    ])


def cite(store, key):
    """取某个维度对应证据的引用标记。走的是生产代码的 id 生成逻辑。"""
    return next(e.citation for e in store.evidences if e.key == key)


# --------------------------------------------------------------------------
# EvidenceStore
# --------------------------------------------------------------------------

def test_evidence_id_is_stable_and_content_addressed(store):
    a = make_evidence_id("https://x.test", "pricing", "$49")
    b = make_evidence_id("https://x.test", "pricing", "$49")
    c = make_evidence_id("https://x.test", "pricing", "$50")
    assert a == b, "同内容必须产出同 id，否则重生成会全表失效"
    assert a != c, "内容变了必须换 id，否则旧 id 会继续代表新内容"


def test_evidence_id_delimiter_prevents_concatenation_collision():
    """NUL 分隔：`a`+`bc` 与 `ab`+`c` 不能撞成同一个 id。"""
    assert make_evidence_id("u", "a", "bc") != make_evidence_id("u", "ab", "c")


def test_store_dedupes_identical_evidence(store):
    duplicated = EvidenceStore.from_extracted_data([
        {"key": "pricing", "value": "$49/席位/月", "source_url": "https://a.test"},
        {"key": "pricing", "value": "$49/席位/月", "source_url": "https://a.test"},
    ])
    assert len(duplicated) == 1


def test_store_skips_empty_values():
    empty = EvidenceStore.from_extracted_data([
        {"key": "pricing", "value": "   ", "source_url": "https://a.test"},
        {"key": "pricing", "value": None, "source_url": "https://a.test"},
    ])
    assert len(empty) == 0
    assert "没有可引用的结构化证据" in empty.table()


def test_store_table_is_compact_and_keeps_ids():
    long_value = "支持 " + "、".join(f"模型{i}" for i in range(1, 30))
    compact = EvidenceStore.from_extracted_data([
        {"key": "models", "value": long_value, "source_url": "https://a.test/models"},
    ])

    table = compact.table()
    assert compact.evidences[0].citation in table
    assert "https://a.test/models" in table
    # 紧凑：不得把完整原文整段塞进提示词
    assert long_value not in table
    assert "…" in table


def test_short_evidence_is_kept_whole_in_table(store):
    """短证据不需要截断——截了反而让模型少看到信息。"""
    table = store.table()
    assert "$49/席位/月" in table


# --------------------------------------------------------------------------
# 数值与句子切分
# --------------------------------------------------------------------------

def test_extract_numbers_normalizes_thousands_separator():
    assert extract_numbers("定价 $1,000/月，支持 3 类模型") == ["1000", "3"]


def test_only_sentences_with_numbers_require_citation(store):
    """概括句强制引用会逼模型编造来源——只有含数值的句子才算事实主张。"""
    claims = iter_claims("该产品定位清晰，服务中大型企业。它定价 $49/席位/月。")
    assert len(claims) == 1
    assert claims[0][1] == ["49"]


def test_chinese_yearless_words_are_not_numbers(store):
    """汉字数字不该被当成可核验数值主张（"第一"不是可核验数据）。"""
    assert iter_claims("排名第一，服务最好") == []


# --------------------------------------------------------------------------
# 四指标
# --------------------------------------------------------------------------

def test_clean_report_passes_all_four_metrics(store):
    p = cite(store, "pricing")
    report = (
        f"## 定价\n\n标准版定价 $49/席位/月 {p}，企业版 $99/席位/月 {p}。\n"
    )
    result = verify_report(report, store)

    assert result["passed"] is True
    assert result["citation_coverage"] == 1.0
    assert result["citation_validity"] == 1.0
    assert result["grounding"] == 1.0
    assert result["hallucinated_urls"] == []
    assert result["violations"] == []


def test_hallucinated_pricing_is_caught_as_ungrounded(store):
    """编造定价：引用有效（id 存在），但数字在证据里找不到。"""
    p = cite(store, "pricing")
    report = f"## 定价\n\n本产品定价 $9/席位/月 {p}。\n"
    result = verify_report(report, store)

    assert result["passed"] is False
    assert result["grounding"] == 0.0
    assert any(v["type"] == "ungrounded_claim" for v in result["violations"])
    assert any("9" in v["detail"] for v in result["violations"])


def test_partially_fabricated_sentence_fails_grounding(store):
    """半真半假必须判失败：49 是真的，60 QPM 是编的。

    这正是"存在量词 vs 全称量词"的区别——只要有一个数字没有来源，整句就不能算扎根。
    """
    p = cite(store, "pricing")
    report = f"## 能力\n\n定价 $49/席位/月 {p}，限流 60 请求/分钟 {p}。\n"
    result = verify_report(report, store)

    assert result["grounding"] == 0.0
    detail = next(v["detail"] for v in result["violations"] if v["type"] == "ungrounded_claim")
    assert "60" in detail


def test_uncited_numeric_claim_is_caught(store):
    report = "## 定价\n\n本产品定价 $49/席位/月。\n"
    result = verify_report(report, store)

    assert result["passed"] is False
    assert result["citation_coverage"] == 0.0
    assert any(v["type"] == "uncited_claim" for v in result["violations"])


def test_unknown_citation_id_is_caught(store):
    result = verify_report("定价 $49/席位/月 [e:ffffffffffff]。\n", store)

    assert result["citation_validity"] == 0.0
    assert any(v["type"] == "unknown_citation" for v in result["violations"])


def test_hallucinated_url_is_caught_even_when_claim_is_grounded(store):
    """编造来源 URL——数字扎根也救不了这个。"""
    p = cite(store, "pricing")
    report = (
        f"## 定价\n\n定价 $49/席位/月 {p}，详见 https://evil.test/fake-pricing\n"
    )
    result = verify_report(report, store)

    assert result["hallucinated_urls"] == ["https://evil.test/fake-pricing"]
    assert result["passed"] is False


def test_collected_urls_are_not_flagged(store):
    p = cite(store, "models")
    report = f"## 模型\n\n支持 GPT-4、Claude、Gemini 三类模型 {p}。来源：https://acme.test/models\n"
    result = verify_report(report, store)
    assert result["hallucinated_urls"] == []


def test_citation_hex_id_digits_are_not_treated_as_numeric_claims(store):
    """evidence_id 是 12 位十六进制，里面全是数字。

    不剥掉引用标记就会把 `[e:501e2d117056]` 抽成三个"事实数值"，
    扎根性将永远判不通过——这类 bug 只靠肉眼读不出来，必须钉死。
    """
    p = cite(store, "pricing")
    assert any(ch.isdigit() for ch in p), "前提：引用 id 确实含数字"

    result = verify_report(f"标准版定价 $49/席位/月 {p}。\n", store)
    assert result["passed"] is True
    assert result["grounding"] == 1.0


def test_single_digit_claim_does_not_match_a_longer_number(store):
    """子串匹配会给编造的数字发通行证：`$9` 会在证据的 `99` 里"找到"。

    证据 pricing 含 `$49` 与 `$99`，报告若声称 `$9`，必须判未扎根。
    """
    p = cite(store, "pricing")
    result = verify_report(f"本产品定价 $9/席位/月 {p}。\n", store)

    assert result["grounding"] == 0.0
    assert result["passed"] is False


def test_equivalent_numeric_forms_are_grounded(store):
    """`49` 与 `49.00`、`1,000` 与 `1000` 应等价——按数值而非字面比对。"""
    precise = EvidenceStore.from_extracted_data([
        {"key": "pricing", "value": "定价 $49.00/席位/月。", "source_url": "https://a.test"},
    ])
    ev = precise.evidences[0].citation
    assert verify_report(f"定价 $49/席位/月 {ev}。\n", precise)["grounding"] == 1.0
    assert verify_report(f"定价 $49.00/席位/月 {ev}。\n", precise)["grounding"] == 1.0


def test_thousands_separator_is_normalized_in_grounding(store):
    """证据写 `1,000`、报告写 `1000`（或反过来）都应判扎根。"""
    big = EvidenceStore.from_extracted_data([
        {"key": "limits", "value": "上限 1,000 请求/分钟。", "source_url": "https://a.test"},
    ])
    ev = big.evidences[0].citation
    assert verify_report(f"上限 1000 请求/分钟 {ev}。\n", big)["grounding"] == 1.0
    assert verify_report(f"上限 1,000 请求/分钟 {ev}。\n", big)["grounding"] == 1.0


def test_report_without_numeric_claims_passes(store):
    """没有任何可核验数值主张的报告不该被判失败——否则会逼模型编造引用。"""
    result = verify_report("## 结论\n\n该产品定位清晰，服务中大型企业。\n", store)
    assert result["passed"] is True
    assert result["claims_checked"] == 0
    assert result["citation_coverage"] == 1.0


def test_empty_report_passes_rather_than_raising(store):
    result = verify_report("", store)
    assert result["passed"] is True
    assert result["violations"] == []


# --------------------------------------------------------------------------
# 违规清单与重生成反馈
# --------------------------------------------------------------------------

def test_violation_list_names_the_offending_sentence(store):
    result = verify_report("本产品定价 $9/席位/月。另一个数字 3 个。\n", store)
    details = " ".join(v["detail"] for v in result["violations"])
    assert "9" in details and "3" in details


def test_format_violations_produces_actionable_feedback(store):
    result = verify_report("本产品定价 $9/席位/月。\n", store)
    text = format_violations(result)
    assert "未标注证据" in text
    assert "不要靠删掉数字来规避" in text


def test_format_violations_is_empty_when_clean(store):
    assert format_violations({"violations": []}) == ""


def test_verification_is_deterministic(store):
    """同一份报告 + 同一份证据，结论必须完全一致——否则不可作为门禁。"""
    report = f"定价 $49/席位/月 {cite(store, 'pricing')}。另称支持 500 个模型。\n"
    runs = [verify_report(report, store) for _ in range(5)]
    assert all(r == runs[0] for r in runs)


# --------------------------------------------------------------------------
# 门槛
# --------------------------------------------------------------------------

def test_thresholds_are_configurable_and_strict_by_default():
    assert DEFAULT_THRESHOLDS["citation_validity"] == 1.00
    assert DEFAULT_THRESHOLDS["max_hallucinated_urls"] == 0


def test_relaxed_threshold_can_be_passed_in(store):
    report = "本产品定价 $9/席位/月。\n"
    assert verify_report(report, store, {"citation_coverage": 0.0, "grounding": 0.0})["passed"]