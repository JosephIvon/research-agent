"""对比语义与报告差异检测的诚实性测试。

改造前存在三处"代码在说谎"：
1. ``generate_differential_analysis`` 把所有竞品的**并集**宣布为"共同能力"；
2. 所谓"独特优势"直接取竞品的**第一条**功能，从未与他人比较过；
3. "支持模型数"数的是 ``_extract_list`` 的条目数，逗号分隔的字符串被算成 1 个模型。

以及一处文档撒谎：``compare_reports`` 的 docstring 承诺返回 ``changes``，
实现却恒返回 ``[]``。本文件锁死修复后的行为，并保证不会回退到撒谎的实现。
"""
import types

import server
from src.tools.comparison_tool import ComparisonAnalyzer, _normalize_feature, count_models
from src.tools.report_comparator import compare_reports


def _competitor(name, features=(), models=None, limitations=()):
    """构造 align_dimensions() 需要的最小竞品结构。"""
    extracted = []
    if features:
        extracted.append({"key": "核心功能", "value": list(features)})
    if models:
        extracted.append({"key": "支持模型", "value": models})
    if limitations:
        extracted.append({"key": "限制", "value": list(limitations)})
    return {
        "name": name,
        "url": f"https://{name}.example.com",
        "status": "success",
        "extracted_data": extracted,
    }


def _aligned(*competitors):
    return ComparisonAnalyzer(list(competitors)).align_dimensions()


def _common_line(text):
    """取出"共同能力"那一整行，供精确断言。"""
    return next(line for line in text.splitlines() if line.startswith("**共同能力**"))


def _unique_line(text, name):
    return next(line for line in text.splitlines() if line.startswith(f"**{name}**独特优势"))


# ---------- 共同能力：交集，不是并集 ----------

def test_common_features_is_intersection_not_union():
    # SSO 只有 A 有，批量导出只有 B 有，两条都不是共同能力
    aligned = _aligned(
        _competitor("A", ["SSO", "审计日志"]),
        _competitor("B", ["审计日志", "批量导出"]),
    )
    assert ComparisonAnalyzer([]).common_features(aligned) == ["审计日志"]


def test_common_features_empty_when_competitors_share_nothing():
    aligned = _aligned(
        _competitor("A", ["SSO"]),
        _competitor("B", ["审计日志"]),
    )
    assert ComparisonAnalyzer([]).common_features(aligned) == []


def test_common_features_all_shared_features_when_identical():
    aligned = _aligned(
        _competitor("A", ["SSO", "审计日志"]),
        _competitor("B", ["SSO", "审计日志"]),
    )
    assert ComparisonAnalyzer([]).common_features(aligned) == ["SSO", "审计日志"]


def test_common_features_of_single_competitor_is_its_own_features():
    # 单竞品时交集退化为它自己的功能集
    aligned = _aligned(_competitor("A", ["SSO", "审计日志"]))
    assert ComparisonAnalyzer([]).common_features(aligned) == ["SSO", "审计日志"]


def test_common_features_empty_without_competitors():
    assert ComparisonAnalyzer([]).common_features([]) == []


def test_common_features_empty_when_one_competitor_has_no_features():
    aligned = _aligned(
        _competitor("A", ["SSO"]),
        _competitor("B", []),
    )
    assert ComparisonAnalyzer([]).common_features(aligned) == []


def test_common_features_intersection_holds_across_three_competitors():
    aligned = _aligned(
        _competitor("A", ["SSO", "审计日志", "私有部署"]),
        _competitor("B", ["SSO", "审计日志"]),
        _competitor("C", ["SSO", "批量导出"]),
    )
    assert ComparisonAnalyzer([]).common_features(aligned) == ["SSO"]


def test_differential_analysis_common_line_lists_only_shared_features():
    analyzer = ComparisonAnalyzer([
        _competitor("A", ["SSO", "审计日志"]),
        _competitor("B", ["审计日志"]),
    ])
    line = _common_line(analyzer.generate_differential_analysis())
    assert line == "**共同能力**：审计日志"
    assert "SSO" not in line, "并集里的非共有功能被当成了共同能力"


def test_differential_analysis_states_honestly_when_no_common_feature():
    analyzer = ComparisonAnalyzer([
        _competitor("A", ["SSO"]),
        _competitor("B", ["审计日志"]),
    ])
    line = _common_line(analyzer.generate_differential_analysis())
    assert line.startswith("**共同能力**：未识别到所有竞品共有的功能")
    assert "SSO" not in line and "审计日志" not in line, "交集为空时不得回退到并集编造"


# ---------- 独特优势：差集，不是"取第一条" ----------

def test_unique_features_is_difference_not_first_item():
    # A 的第一条功能 SSO 是 B 也有的，不能算 A 的独特优势
    aligned = _aligned(
        _competitor("A", ["SSO", "专属沙箱"]),
        _competitor("B", ["SSO"]),
    )
    assert ComparisonAnalyzer([]).unique_features(0, aligned) == ["专属沙箱"]


def test_unique_features_empty_when_all_features_are_shared():
    aligned = _aligned(
        _competitor("A", ["SSO", "审计日志"]),
        _competitor("B", ["SSO", "审计日志"]),
    )
    analyzer = ComparisonAnalyzer([])
    assert analyzer.unique_features(0, aligned) == []
    assert analyzer.unique_features(1, aligned) == []


def test_unique_features_of_single_competitor_are_all_its_features():
    aligned = _aligned(_competitor("A", ["SSO", "审计日志"]))
    assert ComparisonAnalyzer([]).unique_features(0, aligned) == ["SSO", "审计日志"]


def test_unique_features_empty_for_out_of_range_index():
    aligned = _aligned(_competitor("A", ["SSO"]), _competitor("B", ["SSO"]))
    analyzer = ComparisonAnalyzer([])
    assert analyzer.unique_features(5, aligned) == []
    assert analyzer.unique_features(-1, aligned) == []


def test_unique_features_empty_without_competitors_or_features():
    analyzer = ComparisonAnalyzer([])
    assert analyzer.unique_features(0, []) == []
    aligned = _aligned(_competitor("A", []), _competitor("B", []))
    assert analyzer.unique_features(0, aligned) == []


def test_unique_features_lists_all_differences_not_only_the_first():
    aligned = _aligned(
        _competitor("A", ["SSO", "专属沙箱", "按量计费"]),
        _competitor("B", ["SSO"]),
    )
    assert ComparisonAnalyzer([]).unique_features(0, aligned) == ["专属沙箱", "按量计费"]


def test_differential_analysis_unique_line_uses_real_difference():
    analyzer = ComparisonAnalyzer([
        _competitor("A", ["SSO", "专属沙箱"]),
        _competitor("B", ["SSO"]),
    ])
    text = analyzer.generate_differential_analysis()
    assert _unique_line(text, "A") == "**A**独特优势：专属沙箱"
    # B 没有独有功能，必须如实说明而不是退回"取第一条"
    assert _unique_line(text, "B").startswith("**B**独特优势：未识别到独有功能")


def test_differential_analysis_never_claims_shared_feature_as_advantage():
    analyzer = ComparisonAnalyzer([
        _competitor("A", ["SSO"]),
        _competitor("B", ["SSO"]),
    ])
    text = analyzer.generate_differential_analysis()
    assert _unique_line(text, "A").startswith("**A**独特优势：未识别到独有功能")
    assert _unique_line(text, "B").startswith("**B**独特优势：未识别到独有功能")
    # SSO 只允许出现在"共同能力"里，不允许出现在任何一条独特优势里
    assert all("SSO" not in _unique_line(text, name) for name in ("A", "B")), \
        "所有竞品共有的功能被当成了独特优势"
    assert _common_line(text) == "**共同能力**：SSO"


# ---------- 归一化：大小写/空白/标点差异算同一功能 ----------

def test_normalize_feature_folds_case_punctuation_and_whitespace():
    assert _normalize_feature("SSO 登录") == _normalize_feature("sso-登录。")
    assert _normalize_feature("  OpenAI   SDK  ") == "openai sdk"
    assert _normalize_feature("") == ""
    assert _normalize_feature("，、 ") == ""


def test_common_features_matches_across_case_and_punctuation():
    aligned = _aligned(
        _competitor("A", ["SSO 登录"]),
        _competitor("B", ["sso-登录。"]),
    )
    common = ComparisonAnalyzer([]).common_features(aligned)
    assert len(common) == 1, "大小写/标点差异应归一化为同一功能"
    assert common[0] == "SSO 登录", "应输出首个竞品中的原始文本"


def test_normalization_prevents_false_unique_feature():
    # "SSO 登录" 与 "sso-登录。" 实为同一功能，A 的审计日志才是独有的
    aligned = _aligned(
        _competitor("A", ["SSO 登录", "审计日志"]),
        _competitor("B", ["sso-登录。"]),
    )
    assert ComparisonAnalyzer([]).unique_features(0, aligned) == ["审计日志"]


# ---------- 支持模型数：对去重后的模型名计数 ----------

def test_count_models_splits_comma_separated_string():
    # 改造前整条字符串被当作 1 个模型
    assert count_models(["GPT-4, Claude, Gemini"]) == 3


def test_count_models_counts_list_values():
    assert count_models(["GPT-4", "Claude", "Gemini"]) == 3


def test_count_models_dedupes_case_whitespace_and_repeats():
    assert count_models(["GPT-4", "gpt-4", "  GPT-4  ", "Claude"]) == 2


def test_count_models_ignores_empty_entries():
    assert count_models(["", "   ", "GPT-4"]) == 1


def test_count_models_handles_empty_input():
    assert count_models([]) == 0
    assert count_models(None) == 0


def test_count_models_splits_chinese_and_slash_separators():
    assert count_models(["GPT-4、Claude/Gemini"]) == 3


def test_comparison_table_reports_deduplicated_model_count():
    analyzer = ComparisonAnalyzer([
        _competitor("A", ["SSO"], models="GPT-4, Claude, Gemini"),
        _competitor("B", ["SSO"], models="GPT-4, GPT-4"),
    ])
    rows = [line for line in analyzer.generate_comparison_table().splitlines() if "|" in line]
    # rows[0] 为表头、rows[1] 为分隔行，数据行从 rows[2] 开始
    assert rows[2].split("|")[4].strip() == "3"
    assert rows[3].split("|")[4].strip() == "1"


# ---------- compare_reports：真实的行级修改检测 ----------

def test_changes_pairs_old_and_new_for_replaced_line():
    result = compare_reports("# 标题\n\n质量评级：A 级\n\n结论：可用\n", "# 标题\n\n质量评级：A+ 级\n\n结论：可用\n")
    assert result["changes"], "确有内容修改却返回空 changes（docstring 承诺的字段恒为空）"
    assert {"old": "质量评级：A 级", "new": "质量评级：A+ 级"} in result["changes"]


def test_changes_is_empty_for_identical_reports():
    report = "# 标题\n\n正文一\n\n正文二\n"
    result = compare_reports(report, report)
    assert result["changes"] == [], "相同报告不得制造虚假差异"
    assert result["similarity"] == 1.0


def test_changes_excludes_pure_insertion_and_deletion():
    added = compare_reports("# 标题\n正文\n", "# 标题\n正文\n新增结论\n")
    assert added["changes"] == []
    assert added["additions"] == ["新增结论"]

    removed = compare_reports("# 标题\n正文\n结论\n", "# 标题\n正文\n")
    assert removed["changes"] == []
    assert removed["deletions"] == ["结论"]


def test_changes_joins_multi_line_replace_with_newline():
    result = compare_reports("旧第一行\n旧第二行\n保留\n", "新第一行\n新第二行\n保留\n")
    assert result["changes"] == [{"old": "旧第一行\n旧第二行", "new": "新第一行\n新第二行"}]


def test_changes_fragments_really_exist_in_the_two_reports():
    old_report = "# 标题\n\n定价：免费\n\n结论：一般\n"
    new_report = "# 标题\n\n定价：每月 29 元\n\n结论：一般\n"
    result = compare_reports(old_report, new_report)
    assert result["changes"]
    for change in result["changes"]:
        for fragment in change["old"].split("\n"):
            assert fragment in old_report
        for fragment in change["new"].split("\n"):
            assert fragment in new_report


def test_changes_keeps_existing_additions_deletions_and_similarity_unchanged():
    result = compare_reports("a\nb\nc\n", "a\nB\nc\nd\n")
    assert result["additions"] == ["B", "d"]
    assert result["deletions"] == ["b"]
    assert result["similarity"] == 0.667  # SequenceMatcher.ratio() 的既有取值，保持不变
    assert result["summary"].startswith("报告有较大变化")


# ---------- server.py：不再编造 grade ----------

def test_report_payload_does_not_invent_a_grade(tmp_path):
    path = tmp_path / "research_report_20260101_000000_aaaaaaaa.md"
    path.write_text("# 竞品报告\n\n内容\n", encoding="utf-8")
    payload = server._report_payload(path)
    assert payload["grade"] is None, "真实等级缺失时不得硬编码 B 级"
    assert payload["score"] is None


def test_list_history_does_not_invent_a_grade_when_quality_grade_missing(monkeypatch):
    class _FakeDB:
        def list_reports(self, limit=100, user_id=None):
            return [{
                "id": "r1",
                "user_query": "竞品调研",
                "created_at": "2026-01-01T00:00:00",
                "quality_grade": None,
                "quality_score": None,
                "task_id": "t1",
            }]

    monkeypatch.setattr(server, "get_report_db", lambda: _FakeDB())
    monkeypatch.setattr(server, "_report_files", lambda: [])
    request = types.SimpleNamespace(state=types.SimpleNamespace(user_id="u1", role="user"))

    items = server.list_history.__wrapped__(request)
    assert items[0]["grade"] is None, "quality_grade 为空时列表不得显示 B 级"
    assert items[0]["score"] is None


def test_list_history_keeps_the_real_grade(monkeypatch):
    class _FakeDB:
        def list_reports(self, limit=100, user_id=None):
            return [{
                "id": "r1",
                "user_query": "竞品调研",
                "created_at": "2026-01-01T00:00:00",
                "quality_grade": "A",
                "quality_score": 9.2,
                "task_id": "t1",
            }]

    monkeypatch.setattr(server, "get_report_db", lambda: _FakeDB())
    monkeypatch.setattr(server, "_report_files", lambda: [])
    request = types.SimpleNamespace(state=types.SimpleNamespace(user_id="u1", role="user"))

    items = server.list_history.__wrapped__(request)
    assert items[0]["grade"] == "A"
    assert items[0]["score"] == 9.2