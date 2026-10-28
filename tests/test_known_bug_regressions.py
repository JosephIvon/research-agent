"""历史真实故障的回归测试：分解预算、未公开评分、搜索污染兜底。"""
import asyncio

from src.tools.quality_scorer import QualityScorer
from src.workflow.mvp_workflow import CoordinatorAgent, DECOMPOSE_MAX_TOKENS


class _FakeLLM:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    async def achat_completion(self, prompt, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_decompose_passes_dedicated_large_max_tokens():
    """分解沿用客户端 4096 会被 <think> 吃光并静默退化单步骤。"""
    llm = _FakeLLM('```json\n[{"id":"1","description":"定价"},{"id":"2","description":"功能"}]\n```')
    steps = asyncio.run(CoordinatorAgent(llm).decompose_task("调研", []))

    assert [s["description"] for s in steps] == ["定价", "功能"]
    assert llm.kwargs["max_tokens"] == DECOMPOSE_MAX_TOKENS
    assert llm.kwargs["max_tokens"] > 4096


def test_decompose_accepts_single_object_but_drops_non_tasks():
    llm = _FakeLLM('{"id":"1","description":"单步"}')
    steps = asyncio.run(CoordinatorAgent(llm).decompose_task("调研", []))
    assert len(steps) == 1
    assert steps[0]["description"] == "单步"


def test_decompose_empty_or_non_list_falls_back_but_logs():
    """兜底仍然存在，但不能把空数组当成功返回。"""
    for bad in ("[]", '"not a list"', '{"foo": "bar"}'):
        steps = asyncio.run(CoordinatorAgent(_FakeLLM(bad)).decompose_task("调研", []))
        assert len(steps) == 1
        assert steps[0]["description"] == "分析目标网站内容"


def _comp(value, key="pricing"):
    return {"extracted_data": [{
        "key": key, "value": value, "source_url": "https://a.test/", "confidence": 0.9,
    }]}


def test_unpublished_is_not_counted_as_dimension_data():
    """“未公开”是缺口声明，不是数据；不能把全查不到的报告打满分。"""
    report = QualityScorer([_comp("未公开")]).evaluate()

    assert "定价信息" in report.missing_dimensions
    assert report.overall_score < 4.0
    assert report.is_usable is False


def test_real_value_still_counts():
    report = QualityScorer([_comp("标准版 49 元/月")]).evaluate()
    assert "定价信息" not in report.missing_dimensions
    assert report.dimensions[0].data_count == 1


class _SearchTool:
    pass


def test_polluted_bing_result_falls_back_to_searxng(monkeypatch):
    import src.tools.search_tool as st

    monkeypatch.setattr(st, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(st, "SEARCH_FALLBACK_PROVIDERS", ["searxng"])

    tool = st.SearchTool()
    monkeypatch.setattr(tool, "search_with_bing", lambda q, n: [
        {"url": "https://cve.test/CVE-2024-1", "title": "CVE-2024-1", "content": "vulnerability"},
    ])
    monkeypatch.setattr(tool, "search_with_searxng", lambda q, n: [
        {"url": "https://meicai.test/", "title": "美菜", "content": "食材供应链"},
    ])

    results = tool.search("美菜", provider="bing")
    assert [r["url"] for r in results] == ["https://meicai.test/"]


def test_all_irrelevant_results_return_empty_not_pollution(monkeypatch):
    import src.tools.search_tool as st

    monkeypatch.setattr(st, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(st, "SEARCH_FALLBACK_PROVIDERS", [])
    tool = st.SearchTool()
    monkeypatch.setattr(tool, "search_with_bing", lambda q, n: [
        {"url": "https://cve.test/x", "title": "CVE", "content": "security"},
    ])

    assert tool.search("美菜", provider="bing") == []


def test_long_chinese_query_keeps_relevant_results(monkeypatch):
    """长中文整句不能把结果全过滤掉——那是比污染更糟的静默失效。"""
    import src.tools.search_tool as st

    monkeypatch.setattr(st, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(st, "SEARCH_FALLBACK_PROVIDERS", [])
    tool = st.SearchTool()
    monkeypatch.setattr(tool, "search_with_bing", lambda q, n: [
        {
            "url": "https://meicai.test/",
            "title": "美菜 - 生鲜 B2B 供应链",
            "content": "美菜自建仓配网络，川为冻品按区域定价，仓配模式与定价打法对比",
        },
    ])

    query = "对比美菜与川为冻品的仓配模式与定价打法"
    results = tool.search(query, provider="bing")
    assert [r["url"] for r in results] == ["https://meicai.test/"]


def test_long_chinese_query_drops_unrelated_results(monkeypatch):
    """长查询同样要挡住无关页面：靠二元组覆盖度，不靠整串匹配。"""
    import src.tools.search_tool as st

    monkeypatch.setattr(st, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(st, "SEARCH_FALLBACK_PROVIDERS", [])
    tool = st.SearchTool()
    monkeypatch.setattr(tool, "search_with_bing", lambda q, n: [
        {"url": "https://cve.test/x", "title": "CVE-2024", "content": "vulnerability advisory"},
    ])

    query = "对比美菜与川为冻品的仓配模式与定价打法"
    assert tool.search(query, provider="bing") == []
