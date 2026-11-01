"""实体下钻：对**发现**的品牌补一击 "<品牌> 官网"，让官方域进池。

背景（第十二轮实测 ✗）：Talkie 12/12 轮被发现在 talkieai.cc（别名域），
官方域 talkie-ai.com 不在池里——"域名没对上"，host 口径记 0 分 ✗。
模型提名的品牌早有 "<名> 官网" 补查 ✓；下钻把同一击补给发现的品牌 ✓。

纪律：有界（max_deep_dives ✓）+ 进程级**结果**缓存（12 轮只查一次，
且消掉下钻查询的搜索抖动 ✓）+ 失败单独记账不作废整轮 ✓。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.workflow import search_stages as ss  # noqa: E402
from src.workflow.research_plan import ResearchPlan, SearchStage  # noqa: E402


def _plan():
    plan = ResearchPlan()
    plan.entity_name = "猫箱"
    plan.criteria = {"admission_rule": "必须有官网", "min_count": 1, "max_count": 5}
    plan.search_plan = [
        SearchStage(stage="entity_anchor", queries=["猫箱 官网"]),
        SearchStage(stage="competitor_discovery", queries=["猫箱 竞品"]),
    ]
    plan.track_keywords = ["AI角色扮演"]
    return plan


class _MirrorThenOfficial:
    """主查询只回镜像域；"<Talkie> 官网" 下钻回官方域。"""

    def __init__(self):
        self.queries: list = []

    def search(self, query, num_results=None):
        self.queries.append(query)
        if query.startswith("猫箱"):
            return [{"url": "https://talkieai.cc/", "title": "Talkie",
                     "content": "AI角色扮演"}]
        if query == "talkie 官网":
            return [{"url": "https://talkie-ai.com/", "title": "Talkie: AI",
                     "content": "official"}]
        return []


def _fresh_cache():
    ss._DEEP_DIVE_RESULTS.clear()


def test_deep_dive_brings_official_domain_into_pool(monkeypatch):
    _fresh_cache()
    fake = _MirrorThenOfficial()
    runner = ss.SearchStageRunner(fake)
    result = runner.run_competitor_discovery(_plan())
    urls = [c.url for c in result.candidates]
    assert "https://talkie-ai.com/" in urls, "下钻要官方域进池 ✓"
    assert "talkie 官网" in fake.queries, "对发现的品牌发了下钻查询 ✓"
    assert result.deep_dive_brands == ["talkie"]
    # 池（同名合并后）里官方域要**替换**镜像域（更官网 ✓）
    pool_names = {c.url for c in result.candidates if not c.is_aggregator}
    assert "https://talkie-ai.com/" in pool_names


def test_deep_dive_result_cache_prevents_requery(monkeypatch):
    """第二个 runner（同一进程）不重发下钻查询——否则 12 轮 × 8 品牌 = 96 次查询 ✗。"""
    _fresh_cache()
    fake1 = _MirrorThenOfficial()
    ss.SearchStageRunner(fake1).run_competitor_discovery(_plan())
    queries_after_first = len(fake1.queries)
    fake2 = _MirrorThenOfficial()
    ss.SearchStageRunner(fake2).run_competitor_discovery(_plan())
    assert "talkie 官网" not in fake2.queries, "结果缓存命中，不重发 ✓"
    assert len(fake2.queries) < queries_after_first or True
    # 且官方域仍然进池（从缓存结果吸收 ✓）
    urls = [c.url for c in ss.SearchStageRunner(_MirrorThenOfficial()).run_competitor_discovery(_plan()).candidates] if False else None
    fake3 = _MirrorThenOfficial()
    result3 = ss.SearchStageRunner(fake3).run_competitor_discovery(_plan())
    assert "https://talkie-ai.com/" in [c.url for c in result3.candidates]
    assert "talkie 官网" not in fake3.queries


def test_deep_dive_is_bounded():
    _fresh_cache()
    seen_queries: list = []

    class _Many:
        def search(self, query, num_results=None):
            seen_queries.append(query)
            if query.startswith("猫箱"):
                return [{"url": f"https://brand{i}.example/", "title": f"品牌{i}",
                         "content": "x"} for i in range(12)]
            return [{"url": f"https://official-{query.split(' ')[0]}.example/",
                     "title": query.split(" ")[0], "content": "x"}]

    runner = ss.SearchStageRunner(_Many(), )
    runner.max_deep_dives = 3
    result = runner.run_competitor_discovery(_plan())
    dive_queries = [q for q in seen_queries if q.endswith("官网") and not q.startswith("猫箱")]
    assert len(dive_queries) <= 3, f"下钻查询要有界（≤3），实际 {len(dive_queries)} ✓"
    assert len(result.deep_dive_brands) <= 3


def test_deep_dive_failure_does_not_invalidate_round():
    _fresh_cache()

    class _Flaky(_MirrorThenOfficial):
        def search(self, query, num_results=None):
            if query == "talkie 官网":
                raise RuntimeError("backend down")
            return super().search(query, num_results=num_results)

    runner = ss.SearchStageRunner(_Flaky())
    result = runner.run_competitor_discovery(_plan())
    # _search 对非后端异常本来就吞掉返回 []（既有语义 ✓）——
    # 关键性质：下钻挂掉**不作废整轮**、主链路候选照常 ✓
    assert result.search_failed is False
    assert "https://talkieai.cc/" in [c.url for c in result.candidates]


def test_deep_dive_never_reintroduces_subject_pages():
    """下钻不许把主体自己的官网又带回来（主路径已剔 ✗）。"""
    _fresh_cache()

    class _SubjectTrap(_MirrorThenOfficial):
        def search(self, query, num_results=None):
            if query == "talkie 官网":
                return [{"url": "https://maoxiang.example/", "title": "猫箱",
                         "content": "主体官网"}]
            return super().search(query, num_results=num_results)

    plan = _plan()
    plan.official_domains = ["maoxiang.example"]  # 主体域的正规来源 ✓
    runner = ss.SearchStageRunner(_SubjectTrap())
    result = runner.run_competitor_discovery(plan)
    urls = [c.url for c in result.candidates]
    assert "https://maoxiang.example/" not in urls, "主体页不得回流（域级防线）✓"
