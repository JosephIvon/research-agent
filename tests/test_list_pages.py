"""C3 目录页通道的测试（抓取与抽取）。

实测依据（2026-11-01）：
- aigc.cn 的 /favorites/* 集合页是**服务端渲染**的，条目长这样：
  <a href="外链" title="描述"><strong>产品名</strong>…</a> ✓
- 目录站**普遍没有 JSON-LD**（4/4 站实测无）✗ → 只能按 HTML 结构抽。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools import list_pages as lp  # noqa: E402

CARD_HTML = """
<html><body>
<div class="card">
  <a href="https://island.xiaoice.com/" target="_blank" title="小冰岛是一个人工智能实验平台">
    <div class="url-info"><div class="text-sm"><strong>小冰岛</strong></div>
    <p>提供虚拟岛屿环境</p></div></a>
</div>
<div class="card">
  <a href="https://wantalk.baidu.com/" title="万话是一款创新的 AI 对话产品">
    <div class="url-info"><strong>万话</strong></div></a>
</div>
<footer>
  <a href="https://www.ghxi.com/">果核剥壳</a>
  <a href="https://beian.miit.gov.cn/">蜀ICP备2022019184号-2</a>
  <a href="https://qq.com/">QQ</a>
</footer>
</body></html>
"""


def test_extract_entries_reads_name_and_description():
    entries = lp.extract_entries(CARD_HTML, "https://www.aigc.cn/favorites/ai-role-play-chatbot")
    names = {e.name for e in entries}
    assert "小冰岛" in names and "万话" in names
    by_name = {e.name: e for e in entries}
    assert by_name["小冰岛"].url == "https://island.xiaoice.com/"
    assert "人工智能实验平台" in by_name["小冰岛"].description


def test_footer_and_boilerplate_links_are_not_entries():
    """页脚友链/备案链接不是产品条目（实测 ai-bot.cn 的页脚把结果全污染了）。"""
    entries = lp.extract_entries(CARD_HTML, "https://www.aigc.cn/favorites/ai-role-play-chatbot")
    urls = {e.url for e in entries}
    assert "https://www.ghxi.com/" not in urls
    assert not any("miit.gov.cn" in u for u in urls)
    assert "https://qq.com/" not in urls


def test_generic_anchor_text_falls_back_to_title_or_domain():
    """目录站里锚文本常是"访问/Visit"这种按钮词 → 名字退回 title 或域名标签。"""
    html = '<a href="https://www.soulmaite.io/?ref=aitoolnet.com" title="Soulmaite - AI 伴侣">访问</a>'
    entries = lp.extract_entries(html, "https://www.aitoolnet.com/zh/soulmaite")
    assert len(entries) == 1
    assert entries[0].name.lower() in ("soulmaite", "www")
    assert "ref=" not in entries[0].url


def test_tracking_params_are_stripped():
    url = lp._strip_tracking_params("https://x.ai/app?utm_source=aigc.cn&spm=1&id=7")
    assert "utm_source" not in url and "spm" not in url
    assert "id=7" in url


def test_is_listing_shape_uses_path_only():
    assert lp.is_listing_shape("https://www.aigc.cn/favorites/ai-chatbot") is True
    assert lp.is_listing_shape("https://www.aigc.cn/blog/some-article") is False
    assert lp.is_listing_shape("https://example.com/") is False


def test_find_category_pages_matches_track_keywords():
    html = """
    <a href="/favorites/ai-role-play-chatbot">AI角色扮演</a>
    <a href="/favorites/image-generation">AI绘画</a>
    <a href="/blog/hello">博客</a>
    """
    pages = lp.find_category_pages(html, "https://www.aigc.cn/", ["AI 角色扮演"], limit=5)
    assert pages == ["https://www.aigc.cn/favorites/ai-role-play-chatbot"]


def test_collect_channel_items_uses_site_search_and_returns_items(monkeypatch):
    """通道的三步：搜目录站 → site: 找分类页 → 抓分类页抽条目。"""
    seen_queries = []

    def fake_search(query):
        seen_queries.append(query)
        if query.startswith("site:"):
            return [{"url": "https://www.aigc.cn/favorites/ai-role-play-chatbot",
                     "title": "AI角色扮演"}]
        return [{"url": "https://www.aigc.cn/", "title": "AIGC工具导航"}]

    def fake_fetch(url, timeout=20):
        if "aigc.cn/favorites" in url:
            return CARD_HTML, ""
        return "<html><body><a href='/x'>x</a></body></html>", ""

    monkeypatch.setattr(lp, "fetch_html", fake_fetch)
    items, notes, pages = lp.collect_channel_items(
        fake_search, ["AI 角色扮演"], max_homepages=1, max_category_pages=2)
    assert any(q.startswith("site:") for q in seen_queries)
    assert "https://www.aigc.cn/favorites/ai-role-play-chatbot" in pages
    names = {i["title"] for i in items}
    assert "小冰岛" in names
    assert all({"title", "url", "content"} <= set(i) for i in items)


def test_collect_channel_items_without_keywords_is_a_noop(monkeypatch):
    monkeypatch.setattr(lp, "fetch_html", lambda *a, **k: ("", "x"))
    items, notes, pages = lp.collect_channel_items(lambda q: [], [])
    assert items == [] and pages == []
    assert any("跳过" in n for n in notes)


def test_fetch_html_returns_error_instead_of_raising(monkeypatch):
    """抓不到页不能抛异常（通道失败不能拖垮整轮）。"""
    import requests

    def boom(*args, **kwargs):
        raise requests.exceptions.SSLError("boom")

    monkeypatch.setattr(requests, "get", boom)
    html, error = lp.fetch_html("https://example.com/")
    assert html == ""
    assert "SSLError" in error
