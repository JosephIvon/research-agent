"""C4 应用商店通道：host 判定 / App 键去重 / 列表页抽取 / 官网富化 / 失败隔离。

实测依据（2026-11-01）：
- 商店 Web 版详情/榜单页是服务端渲染 ✓（App Store 596KB、小米 26KB、豌豆荚 146KB）；
- 同一 App 的多地区 URL 共享 app id（cn/us/tw/… 六个区 ✗ 要并成一条 ✓）；
- 商店 URL 匹配不上金标准（金标准记产品自己的域）、过不了"必须有官网"的闸门
  ⇒ 详情页里的**开发者官网链接**必须抽出来替换 URL ✓（实测筑梦岛 h5.zhumengdao.com）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import store_pages as sp  # noqa: E402


# -- 主机与形态判定 --------------------------------------------------------

def test_store_host_and_shape_detection():
    assert sp.store_host_of("https://apps.apple.com/cn/app/x/id1") == "apps.apple.com"
    assert sp.store_host_of("https://app.mi.com/details?id=com.a") == "app.mi.com"
    assert sp.store_host_of("https://example.com/") is None
    assert sp.is_app_detail_url("https://apps.apple.com/cn/app/星野/id6465838500")
    assert not sp.is_app_detail_url("https://apps.apple.com/cn/genre/ios-entertainment/id6016")
    assert sp.is_store_listing_url("https://www.wandoujia.com/category/6006")
    assert sp.is_store_listing_url("https://apps.apple.com/cn/charts/ios/7014")
    assert not sp.is_store_listing_url("https://apps.apple.com/cn/app/x/id1")


def test_store_app_key_merges_regions_of_one_app():
    """同一 App 的 cn/us 区共享一个 id ⇒ 一键 ⇒ 只留一条 ✓。"""
    cn = "https://apps.apple.com/cn/app/筑梦岛/id6465838500"
    us = "https://apps.apple.com/us/app/zhumengdao/id6465838500"
    assert sp.store_app_key(cn) == sp.store_app_key(us) == ("apps.apple.com", "id6465838500")
    assert sp.store_app_key("https://app.mi.com/details?id=com.zmd") == \
        ("app.mi.com", "com.zmd")


# -- 列表页抽取 ------------------------------------------------------------

_CATEGORY_HTML = """
<html><body>
<a href="https://www.wandoujia.com/apps/com.foo.bar">魔法AI伙伴</a>
<a href="/apps/com.star.app">星野AI角色</a>
<a href="https://www.wandoujia.com/apps/com.foo.bar">魔法AI伙伴（重复锚）</a>
<a href="https://www.wandoujia.com/category/6006">回到分类</a>
<a href="https://www.wandoujia.com/apps/8118147"><img src="x.png"></a>
</body></html>
"""


def test_extract_app_anchors_from_category_page():
    out = sp.extract_app_anchors(_CATEGORY_HTML,
                                 "https://www.wandoujia.com/category/6006",
                                 "www.wandoujia.com")
    names = [e["name"] for e in out]
    urls = [e["url"] for e in out]
    assert "魔法AI伙伴" in names and "星野AI角色" in names
    assert "https://www.wandoujia.com/category/6006" not in urls
    assert urls.count("https://www.wandoujia.com/apps/com.foo.bar") == 1  # 锚去重 ✓
    assert all("/apps/" in u for u in urls)


# -- 官网链接抽取 ----------------------------------------------------------

_DETAIL_HTML = """
<html><body>
<a href="https://weibo.com/somedeveloper">官方微博</a>
<a href="https://support.apple.com/zh-cn/contact">联系我们</a>
<a href="https://h5.example-app.cn">点击查看</a>
<a href="https://blog.example-app.cn/about">关于我们</a>
</body></html>
"""


def test_extract_official_site_prefers_hint_anchor_and_skips_platforms():
    official = sp.extract_official_site(_DETAIL_HTML, "apps.apple.com")
    assert official == "https://h5.example-app.cn"  # 提示锚优先 ✓
    # 平台域（微博/苹果支持）永远不会被当成官网 ✓
    assert "weibo.com" not in (official or "")
    assert "apple.com" not in (official or "")


# -- 通道整装（失败隔离 + 两条路 + 官网富化） -------------------------------

class _FakeSearch:
    def __init__(self):
        self.queries = []

    def __call__(self, query, num_results=None):
        self.queries.append(query)
        if query.startswith("site:apps.apple.com"):
            # 同一 App 的两个区 + 另一个 App：应并成 2 条 ✓
            return [
                {"url": "https://apps.apple.com/cn/app/星野/id6465838500",
                 "title": "星野", "content": "AI角色扮演"},
                {"url": "https://apps.apple.com/us/app/zhumengdao/id6465838500",
                 "title": "Zhumengdao", "content": "x"},
                {"url": "https://apps.apple.com/cn/app/鲸岛AI/id6790736316",
                 "title": "鲸岛AI", "content": "AI陪伴"},
            ]
        if query.startswith("site:www.wandoujia.com"):
            return [{"url": "https://www.wandoujia.com/category/6006",
                     "title": "分类", "content": ""}]
        return []


@pytest.fixture()
def _patch_fetch(monkeypatch):
    def fake_fetch(url, timeout=20):
        if "/category/" in url:
            return _CATEGORY_HTML, ""
        if "apps.apple.com" in url:
            return _DETAIL_HTML, ""
        return "", f"HTTP 404 ({url[:40]})"

    monkeypatch.setattr(sp, "fetch_html", fake_fetch)
    sp._STORE_SEARCH_CACHE.clear()
    sp._STORE_ENTRIES_CACHE.clear()
    sp._OFFICIAL_SITE_CACHE.clear()


def test_collect_store_items_merges_regions_harvests_and_enriches(_patch_fetch):
    items, notes, pages_read = sp.collect_store_items(
        _FakeSearch(), ["AI角色扮演"], max_detail_fetches=5)
    urls = [i["url"] for i in items]
    titles = [i["title"] for i in items]
    # ① 详情页直取（多地区并成一条 ✓——"星野"的 cn/us 两区只剩一条）
    assert titles.count("星野") == 1
    assert titles.count("鲸岛AI") == 1
    # ② 官网富化把商店 URL 换成开发者域（所以原 URL 里不再有 apps.apple.com ✓）
    assert "https://h5.example-app.cn" in urls
    assert not any("apps.apple.com" in u for u in urls)
    # ③ 列表页收割 ✓
    assert sum(1 for u in urls if "wandoujia.com/apps/" in u) == 2
    # ④ 读过的答案页要记账（列表页 ✓）
    assert pages_read == ["https://www.wandoujia.com/category/6006"]
    assert any("官网" in n for n in notes)


def test_collect_store_items_isolates_backend_failures(_patch_fetch):
    """site: 查询抛后端错误 → 记一条说明，**不炸、不污染其它店** ✓。"""
    class _Flaky(_FakeSearch):
        def __call__(self, query, num_results=None):
            if query.startswith("site:app.mi.com"):
                raise RuntimeError("backend down")
            return super().__call__(query, num_results=num_results)

    items, notes, _ = sp.collect_store_items(_Flaky(), ["AI角色扮演"])
    assert any("app.mi.com" in n and "失败" in n for n in notes)
    assert items, "其它商店的条目不受影响 ✓"


# -- 管线接线：通道条目进候选池、失败不作废整轮 -----------------------------

def test_store_channel_items_enter_candidate_pool(monkeypatch):
    from src.workflow import search_stages as ss
    from src.workflow.research_plan import ResearchPlan, SearchStage

    class _MainAndStore:
        def search(self, query, num_results=None):
            if query.startswith("site:"):
                return [{"url": "https://apps.apple.com/cn/app/星野/id6465838500",
                         "title": "星野", "content": "AI角色扮演"}]
            return [{"url": "https://xingye.example/", "title": "星野",
                     "content": "AI角色扮演应用"}]

    def fake_fetch(url, timeout=20):
        return "", "HTTP 404"

    import src.tools.store_pages as sp2
    monkeypatch.setattr(sp2, "fetch_html", fake_fetch)
    sp2._STORE_SEARCH_CACHE.clear()
    sp2._STORE_ENTRIES_CACHE.clear()
    sp2._OFFICIAL_SITE_CACHE.clear()

    runner = ss.SearchStageRunner(_MainAndStore())
    plan = ResearchPlan()
    plan.entity_name = "猫箱"
    plan.criteria = {"admission_rule": "必须有官网", "min_count": 1, "max_count": 5}
    plan.search_plan = [
        SearchStage(stage="entity_anchor", queries=["猫箱 官网"]),
        SearchStage(stage="competitor_discovery", queries=["猫箱 竞品"]),
    ]
    plan.track_keywords = ["AI角色扮演"]
    runner.run_entity_anchor(plan)
    result = runner.run_competitor_discovery(plan)
    urls = [c.url for c in result.candidates]
    assert any("apps.apple.com" in u for u in urls), "商店条目要进候选池 ✓"
    assert result.search_failed is False, "通道失败不作废整轮 ✓"
    assert result.to_dict()["store_candidates"]
