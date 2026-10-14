"""引用校验闭环的测试。

关键设计：所有断言都打在**生产代码**上（`src/agent/citations.py`），
不在测试里重算一遍判定逻辑——那是"自测自"，校验器写错了测试也跟着绿。
"""
import pytest

from src.agent.citations import (
    CITATION_RE,
    DEFAULT_THRESHOLDS,
    DERIVATION_CONSTANTS,
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
# 句末引用归属（真实模型输出的形态）
#
# 提示词明确要求「引用标在句末」，模型就把 `[e:id]` 放在句号之后：
#     `企业版定价 99 美元/席位/月。[e:abc]`
# 旧切分在句号处断开，于是引用被甩进下一段裸碎片，上一句变成"含数字但无引用"，
# 实测把 citation_coverage 从 1.0 压到 0.5、grounding 压到 0.0——全是解析器的锅。
# --------------------------------------------------------------------------

def test_citation_after_period_belongs_to_previous_sentence():
    report = "企业版定价 99 美元/席位/月。[e:59ed90a05821]\n"
    claims = iter_claims(report)
    assert len(claims) == 1, "裸引用碎片必须归并回前一句，不能变成独立片段"
    assert claims[0][1] == ["99"]
    assert "[e:59ed90a05821]" in claims[0][0]


def test_leading_citation_fragment_does_not_create_a_phantom_claim():
    """引用碎片在报告开头时无处可归并，必须安静丢弃而不是凭空造一条主张。"""
    assert iter_claims("[e:59ed90a05821] 定价清晰。\n") == []


def test_trailing_citation_counts_as_cited(store):
    p = cite(store, "pricing")
    report = f"企业版定价 $99/席位/月。{p}\n"
    result = verify_report(report, store)
    assert result["citation_coverage"] == 1.0
    assert result["grounding"] == 1.0
    assert result["passed"]


def test_inline_citation_still_works_after_merging(store):
    """归并不能吃掉句内引用——两种写法都要算已引用。"""
    p = cite(store, "pricing")
    for report in (
        f"企业版定价 $99/席位/月 {p}。\n",
        f"企业版定价 $99/席位/月。{p}\n",
    ):
        assert verify_report(report, store)["citation_coverage"] == 1.0


def test_uncited_number_sentence_is_still_flagged(store):
    """归并只搬运引用，不能顺手把真·没引用的句子也判成已引用。"""
    report = f"企业版定价 $99/席位/月。另有 7 个集成。{cite(store, 'pricing')}\n"
    result = verify_report(report, store)
    assert result["citation_coverage"] == 0.5


# --------------------------------------------------------------------------
# Markdown 结构编号不是事实主张
#
# 报告用 `### 2.1 对比概览`、`1. **Acme**：…` 组织结构。这些数字是**章节编号**，
# 拿它们去证据原文里找必然找不到，会把扎根性直接打到 0（实测列表项 `1.` 就是元凶）。
# --------------------------------------------------------------------------

def test_heading_section_number_is_not_a_claim():
    assert iter_claims("### 2.1 对比概览\n") == []
    assert iter_claims("## 三、竞品横向对比\n") == []


def test_ordered_list_marker_is_not_a_claim():
    assert iter_claims("1. 结论先行\n2. 其次补充\n") == []


def test_list_marker_is_stripped_but_body_numbers_stay():
    report = "1. **Acme**：标准版定价 $49/席位/月 [e:59ed90a05821]。\n"
    claims = iter_claims(report)
    assert len(claims) == 1
    assert claims[0][1] == ["49"], "列表标记 1 不是事实数值，不能进扎根性比对"


def test_heading_year_is_still_a_numeric_claim():
    """只剥「结构编号」，不剥正文数字：`## 2024 年度总结` 的 2024 仍是主张。"""
    claims = iter_claims("## 2024 年度总结\n")
    assert [n for _, n in claims] == [["2024"]]


def test_numbered_body_sentence_keeps_all_numbers():
    report = "1. 第一季度营收 1200 万美元，同比增长 35%。\n"
    claims = iter_claims(report)
    assert claims == [("第一季度营收 1200 万美元，同比增长 35%", ["1200", "35"])]


def test_structure_numbers_alone_do_not_drag_grounding_to_zero(store):
    p = cite(store, "pricing")
    report = (
        "### 2.1 对比概览\n"
        "1. 标准版定价 $49/席位/月 " + p + "。\n"
        "2. 支持三类模型 " + cite(store, "models") + "。\n"
    )
    result = verify_report(report, store)
    assert result["grounding"] == 1.0
    assert result["citation_coverage"] == 1.0
    assert result["passed"], result["violations"]


# --------------------------------------------------------------------------
# 无引用句的性质分类：真·无据断言 vs 已识别缺口
#
# 「含数字且没引用」有两种性质完全不同的句子：
#   断言：Acme 定价 39 美元/席位/月        ← 陈述外部事实却没给来源，是真指控
#   缺口：该限速策略公开页面未披露，建议向厂商索取   ← 承认"没查到"，是系统做对了
#
# 真实 MiniMax 冒烟里被判 uncited 的正是后者，但旧措辞把它写成
# 「含数值的结论未标注证据：…建议向厂商索取确认」——用户读到的像是系统在指控。
#
# **只改呈现，不改判定**：两类都仍然计入 citation_coverage、仍然可能导致
# passed=False。若在此处给缺口句免引用，等于开了一条"写上'未披露'就绕过门禁"
# 的逃逸通道，那是比误报严重得多的问题。
# --------------------------------------------------------------------------

GAP_ACK = "该 60 次/分钟的限速阈值仅适用于免费层；付费层级的限速策略公开页面未披露，建议向厂商索取书面 SLA 确认"
ASSERTION = "Acme 标准版定价 39 美元/席位/月"


def test_gap_acknowledgement_is_classified_as_gap_not_assertion(store):
    r = verify_report(f"**容量评估**：{GAP_ACK}\n", store)
    uncited = [v for v in r["violations"] if v["type"] == "uncited_claim"]
    assert len(uncited) == 1
    assert uncited[0]["kind"] == "gap_ack", "承认缺口的句子不该被当成无据断言"


def test_plain_assertion_is_classified_as_assertion(store):
    r = verify_report(f"{ASSERTION}\n", store)
    uncited = [v for v in r["violations"] if v["type"] == "uncited_claim"]
    assert len(uncited) == 1
    assert uncited[0]["kind"] == "assertion"


@pytest.mark.parametrize("wording", [
    "付费层限速策略公开页面未披露",
    "该指标未公开，建议索取书面确认",
    "我们未查到官方说明，需向厂商核实",
    "该数值无法核实，建议索取 SLA",
    "定价暂缺，建议向厂商索取报价单",
])
def test_gap_wording_variants_are_recognised(store, wording):
    r = verify_report(f"容量评估：{wording}，其中限速 60 次/分钟。\n", store)
    uncited = [v for v in r["violations"] if v["type"] == "uncited_claim"]
    assert uncited and uncited[0]["kind"] == "gap_ack", wording


def test_classification_never_exempts_from_coverage(store):
    """**安全底线**：被认作缺口的句子照样计入覆盖度、照样拉低分数。

    若这条红了，说明有人把"改呈现"做成了"免引用"——那正是必须拦住的逃逸通道。
    """
    with_gap = verify_report(f"{GAP_ACK}。\n", store)
    without = verify_report(f"{ASSERTION}。\n", store)

    assert with_gap["violation_count"] > 0, "缺口句必须仍然算违规"
    assert with_gap["citation_coverage"] == without["citation_coverage"], \
        "两类句子对 coverage 的影响必须相同，否则分类就成了免引用后门"
    assert with_gap["passed"] is False


def test_other_violation_types_carry_no_kind(store):
    """只有 uncited_claim 需要分类——无据断言、幻觉 URL 不该被这个措辞带偏。"""
    r = verify_report(f"标准版定价 $9/席位/月 {cite(store, 'pricing')}。\n", store)
    assert all("kind" not in v for v in r["violations"])


def test_gap_detail_must_not_carry_the_accusation_prefix(store):
    """缺口句的 detail 也不能顶着「含数值的结论未标注证据」。

    单列成分组只解决一半——前端、SQLite 里的旧记录、任何按 detail 渲染的消费者
    都会原样把这句读给用户看。措辞必须在源头就换掉。
    """
    r = verify_report(f"容量评估：{GAP_ACK}\n", store)
    detail = next(v["detail"] for v in r["violations"] if v["type"] == "uncited_claim")
    assert "未标注证据" not in detail
    assert "信息缺口" in detail


def test_assertion_detail_keeps_the_accusation_wording(store):
    """对照：真违规的措辞不能被一起软化。"""
    r = verify_report(f"{ASSERTION}\n", store)
    detail = next(v["detail"] for v in r["violations"] if v["type"] == "uncited_claim")
    assert "未标注证据" in detail


def test_format_violations_does_not_tell_model_to_cite_a_gap():
    """给模型的反馈也要区分：让模型去给「建议索取确认」补引用是白烧重生成预算，
    而且补出来的引用语义上是错的。"""
    gap_result = {"violations": [{
        "type": "uncited_claim", "kind": "gap_ack",
        "detail": f"含数值的结论未标注证据：{GAP_ACK}",
    }]}
    text = format_violations(gap_result)
    assert "信息缺口" in text and "不需要补引用" in text
    # 有缺口句时标题不能说"必须逐条修正"——一半的问题根本不用改，
    # 一句「全部必须修正」会让模型去给保留意见硬凑引用。
    assert "必须逐条修正" not in text


def test_format_violations_still_demands_citation_for_real_assertion():
    r = {"violations": [{
        "type": "uncited_claim", "kind": "assertion",
        "detail": f"含数值的结论未标注证据：{ASSERTION}",
    }]}
    text = format_violations(r)
    assert "未标注证据" in text


def test_format_violations_tolerates_missing_kind():
    """老数据（SQLite 里已持久化的 violations）没有 kind 字段，不能因此崩掉。"""
    text = format_violations({"violations": [{"type": "uncited_claim", "detail": "某句"}]})
    assert "某句" in text


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


def test_format_violations_groups_by_type_with_counts():
    """同类违规必须聚合计数——平铺 60 条同型指控会把提示词预算烧光，
    而模型真正需要的是「哪一类错、错了几处」，不是逐条念罪状。"""
    result = {"violations": (
        [{"type": "uncited_claim", "detail": f"缺引用句子{i}"} for i in range(60)]
        + [{"type": "ungrounded_claim", "detail": f"数字{9}无据{i}"} for i in range(10)]
    )}
    text = format_violations(result)

    assert "uncited_claim" in text and "ungrounded_claim" in text
    assert "60" in text and "10" in text
    assert "70" in text, "总处数要让模型知道工作量"
    # 平铺会让行数随违规数线性增长；聚合后必须封顶
    assert len(text.splitlines()) < 20, "清单长度必须与违规数量解耦"


def test_format_violations_keeps_examples_per_type():
    result = {"violations": [
        {"type": "uncited_claim", "detail": f"缺引用句子{i}"} for i in range(8)
    ]}
    text = format_violations(result)
    assert "缺引用句子0" in text, "每类至少留一个可定位的示例"
    assert "缺引用句子7" not in text, "示例不应平铺全部"


def test_format_violations_still_warns_against_deleting_numbers():
    result = {"violations": [{"type": "uncited_claim", "detail": "x"}]}
    assert "不要靠删掉数字来规避" in format_violations(result)


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


# --------------------------------------------------------------------------
# 推导数值：单列 derived_claims，不阻塞交付
#
# 真实 MiniMax 冒烟实测（M3 报告，证据含「年付享 8 折」），两句必须区别对待：
#
#   ①「折算后标准版 39.2 美元/席位/月、企业版 79.2 美元/席位/月」
#      39.2 = 49 × 8 ÷ 10、79.2 = 99 × 8 ÷ 10，是**正确推导**。
#      旧实现按「数值集合精确匹配」把它判成编造，扎根性被误判拉低、门槛形同虚设。
#
#   ②「以 50 人团队、按年付计算，标准版年化成本 23,520 美元」
#      其中 50 是模型自己假设的团队规模，**不在证据里**——这不是推导，是无据假设，
#      必须继续报 ungrounded_claim。
#
# 分不清这两者，「加推导」就等于给"编一个前提再算"发了通行证。
# --------------------------------------------------------------------------

@pytest.fixture
def discount_store():
    """还原真实冒烟里的定价证据：两个价位 + 年付 8 折。"""
    return EvidenceStore.from_extracted_data([
        {
            "key": "pricing",
            "value": "标准版 49 美元/席位/月，企业版 99 美元/席位/月，年付享 8 折。",
            "source_url": "https://acme.test/pricing",
        },
    ])


def test_correct_derivation_is_recorded_instead_of_flagged(discount_store):
    """① 49 × 8 ÷ 10 = 39.2：正确推导，不报无据，进 derived_claims。"""
    p = cite(discount_store, "pricing")
    report = (
        f"同时提供年付 8 折优惠，折算后标准版 39.2 美元/席位/月、"
        f"企业版 79.2 美元/席位/月 {p}\n"
    )
    result = verify_report(report, discount_store)

    ungrounded = [v for v in result["violations"] if v["type"] == "ungrounded_claim"]
    assert ungrounded == [], "正确推导被判编造，扎根性被误伤"
    assert result["grounding"] == 1.0
    assert result["passed"] is True
    assert [d["value"] for d in result["derived_claims"]] == ["39.2", "79.2"]
    assert all(d["sentence"] and d["expression"] for d in result["derived_claims"])


def test_phantom_team_size_is_still_ungrounded(discount_store):
    """② 50 人不在证据里：23520 不是推导，仍必须判无据。

    注意 `50` 恰好等于 `99 - 49`，纯数值校验分不清"两个价位的差"和"团队规模"，
    会被记成一条推导——这是巧合，无害：**门槛由编造出来的 23520 拦下**，
    所以断言钉在 23520 上（它必须无据、且不得出现在 derived_claims 里）。
    """
    p = cite(discount_store, "pricing")
    report = f"以 50 人团队、按年付计算，标准版年化成本 23,520 美元 {p}\n"
    result = verify_report(report, discount_store)

    assert result["grounding"] == 0.0
    assert result["passed"] is False
    detail = next(v["detail"] for v in result["violations"] if v["type"] == "ungrounded_claim")
    assert "23520" in detail
    assert "23520" not in [d["value"] for d in result["derived_claims"]], \
        "编一个前提再算的结果绝不能被记成推导"


def test_derivation_operands_must_come_from_the_cited_evidence(discount_store):
    """操作数只能取自**本句所引**的证据。

    把整个 store 的数字都当操作数，就等于让报告随便引一条证据、便把别处的数字借来
    当"前提"：下面这句里的 `$9` 借到了未引用证据里的 `50`（`9 = (50-49)+8`），
    编造值就被洗成了推导。后门正是从这里开的。
    """
    discount_store.add(url="https://acme.test/team", key="team", value="典型客户团队规模 50 人。")
    p = cite(discount_store, "pricing")
    result = verify_report(f"本产品定价 $9/席位/月 {p}\n", discount_store)

    assert result["grounding"] == 0.0
    assert result["derived_claims"] == []


def test_direct_quote_stays_grounded_and_is_not_recorded_as_derived(discount_store):
    """回归保护：直接引用数值照常判 grounded，且不该被记成推导。"""
    p = cite(discount_store, "pricing")
    report = f"标准版 49 美元/席位/月、企业版 99 美元/席位/月 {p}\n"

    result = verify_report(report, discount_store)
    assert result["grounding"] == 1.0
    assert result["violations"] == []
    assert result["derived_claims"] == [], "原文里有的数字不需要、也不应该被当成推导"


def test_fabricated_numbers_are_still_ungrounded(discount_store):
    """回归保护：加了推导能力不等于放松编造判定。

    `$9` / `60 QPM` / `2352` 在证据 {49, 99, 8} + 常数集下都凑不出合法推导式，
    必须继续判无据——这里防的是"推导判定退化成放行一切"。
    """
    p = cite(discount_store, "pricing")
    for number in ("9", "60", "2352"):
        result = verify_report(f"本产品限流 {number} 请求/分钟 {p}\n", discount_store)
        assert result["grounding"] == 0.0, f"{number} 不该被判成推导"
        assert result["passed"] is False
        assert result["derived_claims"] == []


def test_near_miss_value_is_not_a_derivation(discount_store):
    """容差必须够紧：39.3 与 39.2 差 0.001，不是同一个推导结果。"""
    p = cite(discount_store, "pricing")
    result = verify_report(f"年付折算后标准版 39.3 美元/席位/月 {p}\n", discount_store)

    assert result["grounding"] == 0.0
    assert result["derived_claims"] == []


def test_single_step_derivations_from_the_design_doc_are_recognised():
    """方案文档举的两个例子恰好都只有**一层**运算，反推式的两层实现必须单独兜住它们。

    `20% -> 35%` 写「提升 15 个百分点」是 `35 - 20`；`$10/月` 写 `$120/年` 是 `10 × 12`。
    只做两层反推会把这两个正确推导判成编造，正是本项要修的老毛病。
    """
    growth = EvidenceStore.from_extracted_data([{
        "key": "adoption",
        "value": "客户增长率由 20% 提升至 35%。",
        "source_url": "https://acme.test/adoption",
    }])
    ev = cite(growth, "adoption")
    result = verify_report(f"同期客户增长率提升 15 个百分点 {ev}\n", growth)

    assert result["grounding"] == 1.0
    assert [d["value"] for d in result["derived_claims"]] == ["15"]
    assert "35" in result["derived_claims"][0]["expression"]

    price = EvidenceStore.from_extracted_data([{
        "key": "pricing",
        "value": "标准版定价 $10/席位/月。",
        "source_url": "https://acme.test/pricing",
    }])
    ev = cite(price, "pricing")
    result = verify_report(f"标准版年付约合 120 美元/席位/年 {ev}\n", price)

    assert result["grounding"] == 1.0
    assert [d["value"] for d in result["derived_claims"]] == ["120"]


def test_two_constants_in_one_expression_are_not_a_derivation(discount_store):
    """R3：`49 × 10 × 12 = 5880` 看着能算出来，但没有语义——一个证据数乘两个单位换算常数。

    没有这条规则，`5880` 这种编造值就会被洗成推导。
    """
    p = cite(discount_store, "pricing")
    result = verify_report(f"标准版年化成本 5880 美元 {p}\n", discount_store)

    assert result["grounding"] == 0.0
    assert result["derived_claims"] == []


def test_derived_claim_record_is_auditable(discount_store):
    """记录必须能让人复核这个数是怎么来的：句子、数值、推导式、操作数出处。"""
    st = discount_store
    p = cite(st, "pricing")
    result = verify_report(f"年付折算后标准版 39.2 美元/席位/月 {p}\n", st)

    record = result["derived_claims"][0]
    assert record["sentence"].startswith("年付折算后标准版")
    assert record["value"] == "39.2"
    assert "39.2" in record["expression"]
    assert set(record["operands"]) == {"49", "8", "10"}
    assert record["constants"] == ["10"]
    assert record["evidence_ids"] == [st.evidences[0].evidence_id]
    # 单列不阻塞：推导数不进违规清单，也不改门槛
    assert result["violations"] == []
    assert result["derived_claim_count"] == 1


def test_mixed_sentence_reports_fabrication_but_keeps_the_derived_number(discount_store):
    """一句里同时有直接引用数、推导数、编造数：仍判 ungrounded，推导数要留痕。"""
    p = cite(discount_store, "pricing")
    report = (
        f"标准版 49 美元/席位/月，年付折算后 39.2 美元/席位/月，"
        f"限流 9999 QPM {p}\n"
    )
    result = verify_report(report, discount_store)

    assert result["grounding"] == 0.0
    detail = next(v["detail"] for v in result["violations"] if v["type"] == "ungrounded_claim")
    # detail 会回显整句，所以只断言"被判无据的数值清单"那一段
    accused = detail.split("在所引证据原文中找不到")[0]
    assert "9999" in accused
    assert "39.2" not in accused, "推导数不该混进无据清单"
    assert "49" not in accused, "原文里有的数字更不该出现在无据清单里"
    assert [d["value"] for d in result["derived_claims"]] == ["39.2"]


def test_uncited_claim_is_unaffected_by_derivation(discount_store):
    """没有引用就照旧报无引用——推导能力不能顺带把无引用句"洗白"。"""
    report = "同时提供年付 8 折优惠，折算后标准版 39.2 美元/席位/月。\n"
    result = verify_report(report, discount_store)

    assert result["citation_coverage"] == 0.0
    assert [v["type"] for v in result["violations"]] == ["uncited_claim"]
    assert result["derived_claims"] == []


def test_derivation_constants_are_a_fixed_auditable_set():
    """常数集必须写死且可审计：放宽它就等于允许"编一个前提再算"。"""
    assert set(DERIVATION_CONSTANTS) == {10.0, 12.0, 100.0, 1000.0}
    assert all(isinstance(name, str) and name for name in DERIVATION_CONSTANTS.values())


def test_derivation_detection_is_deterministic(discount_store):
    """推导判定是门禁，必须全确定性——同样的输入永远给同样的结论与同一条推导式。"""
    p = cite(discount_store, "pricing")
    report = (
        f"标准版 49 美元/席位/月，年付折算后 39.2 美元/席位/月，"
        f"企业版 79.2 美元/席位/月 {p}\n"
    )
    runs = [verify_report(report, discount_store) for _ in range(5)]
    assert all(r == runs[0] for r in runs)
    assert runs[0]["derived_claims"]


def test_fabricated_pricing_report_always_trips_the_gate(discount_store):
    """方案文档 §4-M2 验收：编造定价的报告必报幻觉 URL 或 ungrounded_claims > 0。"""
    p = cite(discount_store, "pricing")
    report = (
        f"本产品定价 $9/席位/月 {p}，企业版 $299/席位/月 {p}，"
        f"年付折算后 39.2 美元/席位/月，详见 https://evil.test/cheap\n"
    )
    result = verify_report(report, discount_store)

    ungrounded = [v for v in result["violations"] if v["type"] == "ungrounded_claim"]
    assert result["hallucinated_urls"] or ungrounded
    assert result["passed"] is False