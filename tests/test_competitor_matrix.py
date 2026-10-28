"""竞品矩阵：schema、证据、缺列/缺证据、报告注入的确定性测试。"""
import re

from src.workflow.competitor_matrix import (
    CompetitorMatrix,
    CompetitorRow,
    EVIDENCE_GRADES,
    build_matrix,
    inject_section,
    markdown,
    render_report_section,
    validate,
)


def _comp(name, url, items):
    return {"name": name, "url": url, "status": "success", "extracted_data": items}


def _item(key, value, url="https://a.test/", grade="official", **extra):
    return {"key": key, "value": value, "source_url": url,
            "confidence": 0.9, "evidence_grade": grade, **extra}


def _two_competitors():
    return [
        _comp("Alpha", "https://a.test/", [
            _item("name", "Alpha"),
            _item("positioning", "面向连锁餐饮的冻品供应链"),
            _item("model_points", ["自营仓配", "9 仓覆盖 150 城"]),
            _item("borrowable", ["多仓网络", "外协运力"]),
            _item("as_of", "2026-10-27"),
        ]),
        _comp("Beta", "https://b.test/", [
            _item("name", "Beta"),
            _item("positioning", "冻品 B2B 小程序商城"),
            _item("model_points", "双小程序 + 拆箱送"),
            _item("borrowable", "贴码分拣"),
            _item("as_of", "2026-10-20"),
        ]),
    ]


def test_two_competitors_have_all_required_columns_and_evidence():
    matrix = build_matrix("冻品供应链竞品", competitors=_two_competitors())

    assert [r.platform for r in matrix.rows] == ["Alpha", "Beta"]
    assert all(r.columns_complete for r in matrix.rows)
    assert all(r.evidence for r in matrix.rows)
    assert all(len(e.evidence_id) == 12 for r in matrix.rows for e in r.evidence)
    assert matrix.completeness["rows"] == 2
    assert matrix.completeness["completeness"] == 1.0
    assert matrix.is_complete is True


def test_markdown_has_fixed_columns_and_parseable_citations():
    matrix = build_matrix("冻品供应链竞品", competitors=_two_competitors())
    table = markdown(matrix)
    header = table.splitlines()[0]

    for col in ("平台", "定位", "模式要点", "可借鉴点", "证据", "口径日期", "证据等级"):
        assert col in header
    evidence_ids = re.findall(r"\[e:([0-9a-f]{12})\]", table)
    assert len(evidence_ids) >= 4, "两条竞品都应渲染出可解析的证据引用"


def test_missing_columns_are_explicitly_unverified_not_invented():
    matrix = build_matrix("缺资料竞品", competitors=[
        _comp("Gamma", "https://c.test/", [
            _item("name", "Gamma"),
            _item("pricing", "未公开"),
        ])
    ])
    row = matrix.rows[0]

    assert row.positioning == "未核实"
    assert row.model_points == ["未核实"]
    # 可借鉴点没有证据时只能留占位，不能编一个动作出来
    assert any("待产品判断" in item for item in row.borrowable)
    assert row.as_of == "未核实"
    assert any("缺定位" in w for w in row.warnings)
    assert any("未编造" in w for w in row.warnings)
    # 缺证据的列仍然有值，不能因为“没查到”就省掉列
    assert row.columns_complete is True
    # 但占位行**不是**有效内容：有效完整度必须为假，评测门禁据此拦下
    assert row.content_complete is False
    # 这行的证据本身是有的（name/pricing 有来源），缺的是四个必需列的有效内容
    for missing in ("positioning", "model_points", "borrowable", "as_of"):
        assert missing in row.content_missing


def test_numeric_claim_without_evidence_is_marked_unverified():
    """证据里找不到的数字必须标未核实，而不是当成事实。"""
    from src.workflow.competitor_matrix import _mark_unverified_numbers

    marked, missing = _mark_unverified_numbers("覆盖 150 城", ["覆盖 100 城"])
    assert marked.startswith("[未核实]")
    assert missing == ["150"]

    # 真实矩阵里，缺资料的竞品不会因为列有值就被当成有据
    matrix = build_matrix("数字无据", competitors=[
        _comp("Delta", "https://d.test/", [
            _item("name", "Delta"),
            _item("pricing", "未公开"),
        ])
    ])
    row = matrix.rows[0]
    assert any("待产品判断" in item for item in row.borrowable)
    assert any("未编造" in w or "未核实" in w for w in row.warnings)
    assert row.content_complete is False


def test_numeric_claim_with_evidence_is_not_marked():
    matrix = build_matrix("数字有据", competitors=[
        _comp("Echo", "https://e.test/", [
            _item("name", "Echo"),
            _item("positioning", "多仓平台"),
            _item("model_points", "覆盖 150 城"),
            _item("borrowable", "多仓"),
            _item("coverage", "已覆盖 150 个城市"),
        ])
    ])
    assert not matrix.rows[0].model_points[0].startswith("[未核实]")


def test_conflicting_values_are_flagged_not_silently_merged():
    matrix = build_matrix("冲突", competitors=[
        _comp("Foxtrot", "https://f.test/", [
            _item("name", "Foxtrot"),
            _item("positioning", "高端平台"),
            _item("positioning", "低价平台"),
            _item("model_points", "自营"),
            _item("borrowable", "仓配"),
        ])
    ])
    assert any("冲突" in w for w in matrix.rows[0].warnings)


def test_multi_source_conflict_is_distinguished_from_same_source():
    matrix = build_matrix("跨源冲突", competitors=[
        _comp("Golf2", "https://g2.test/", [
            _item("name", "Golf2"),
            _item("positioning", "高端平台", url="https://official.test/"),
            _item("positioning", "低价平台", url="https://media.test/"),
            _item("model_points", "自营"),
        ])
    ])
    assert any("多来源冲突" in w for w in matrix.rows[0].warnings)


def test_weakest_evidence_grade_wins_for_the_row():
    matrix = build_matrix("等级", competitors=[
        _comp("Golf", "https://g.test/", [
            _item("name", "Golf", grade="official"),
            _item("positioning", "平台", grade="media"),
            _item("model_points", "自营", grade="unverified"),
            _item("borrowable", "仓配", grade="official"),
        ])
    ])
    assert matrix.rows[0].evidence_grade == "unverified"
    assert matrix.rows[0].evidence_grade in EVIDENCE_GRADES


def test_validate_rejects_missing_required_column():
    matrix = CompetitorMatrix(query="x", rows=[
        CompetitorRow(platform="A", positioning="", model_points=["m"], borrowable=["b"])
    ])
    try:
        validate(matrix)
    except Exception as exc:
        assert "缺列 positioning" in str(exc)
    else:
        raise AssertionError("缺列必须抛 MatrixValidationError")


def test_report_section_is_fixed_and_removes_model_written_duplicate():
    matrix = build_matrix("冻品供应链竞品", competitors=_two_competitors())
    report = (
        "## 一、调研概述\n\n这是叙述。\n\n"
        "## 竞品模式速览\n\n模型自己写的散文表，应该被删掉。\n\n"
        "## 二、分析\n\n正文保留。\n"
    )
    out = inject_section(report, matrix)

    assert out.count("## 竞品模式速览") == 1
    assert "模型自己写的散文表" not in out
    assert "<!--machine-appendix-->" in out
    assert "这是叙述。" in out and "正文保留。" in out
    assert "| 平台 | 定位 | 模式要点 | 可借鉴点 | 证据 | 口径日期 | 证据等级 |" in out


def test_empty_matrix_still_renders_a_section_with_unverified_row():
    matrix = CompetitorMatrix(query="无结果")
    section = render_report_section(matrix)
    assert "## 竞品模式速览" in section
    assert "未核实" in section


def test_borrowable_is_derived_from_evidence_backed_model_points():
    """可借鉴点是产品判断：必须标推断、附来源证据，且不是占位。"""
    matrix = build_matrix("冻品供应链竞品", competitors=[
        _comp("川为冻品", "https://www.chuanwei.test/", [
            _item("name", "川为冻品", url="https://www.chuanwei.test/"),
            _item("positioning", "冻品 B2B 低价平台", url="https://www.chuanwei.test/"),
            _item("business_model", ["自营仓配，多仓网络覆盖多个城市", "外协运力承接干线"],
                  url="https://www.chuanwei.test/"),
            _item("as_of", "2026-10-28", url="https://www.chuanwei.test/"),
        ])
    ])
    row = matrix.rows[0]

    assert row.borrowable, "有证据的模式要点必须推出可借鉴点"
    assert all(item.startswith("推断：") for item in row.borrowable)
    assert not any("待产品判断" in item for item in row.borrowable)
    # 每条借鉴点都能追到来源证据 id
    assert all(re.search(r"\[e:[0-9a-f]{12}\]", item) for item in row.borrowable)
    assert any("规则表" in w for w in row.warnings)
    assert row.content_complete is True


def test_derived_borrowable_without_evidence_is_not_invented():
    """没有来源的条目不能推出借鉴点——否则就是凭空编动作。"""
    matrix = build_matrix("无源竞品", competitors=[
        _comp("无名", "https://none.test/", [
            {"key": "business_model", "value": "多仓网络", "source_url": ""},
        ])
    ])
    row = matrix.rows[0]
    assert any("待产品判断" in item for item in row.borrowable)
    assert row.content_complete is False


def test_rule_actions_contain_no_digits():
    """动作文案里的数字会变成“无据数字”，污染未核实标记。"""
    from src.workflow.borrowable_rules import RULES

    offenders = [rule.rule_id for rule in RULES if any(ch.isdigit() for ch in rule.action)]
    assert not offenders, f"动作文案不允许出现数字: {offenders}"
    assert len({rule.rule_id for rule in RULES}) == len(RULES), "rule_id 必须唯一"
