"""页面日期抽取 + 报告「信息时效」小节的测试。

三件要钉住的事：
1. 抽出来的日期**一定是 ISO**（页面内容是攻击者可控的，原样渲染等于把注入面
   带进报告）；
2. 报告里只有**源站声明的日期**，没有抓取时间——抓取时间只说明何时取回，
   把它写进报告会让"最旧日期"这条判据把无源站日期的报告算成最新；
3. 夹具语料**仍横跨三档**（近 / 中 / 过时）——日期是固定值，语料会变旧，
   掉档必须有人被强制看见（否则 freshness 静默退化成常量）。
"""
from datetime import date

import pytest

from src.crawler.page_dates import extract_page_dates
from src.tools.quality_scorer import QualityScorer


# --------------------------------------------------------------------------
# 1) 抽取
# --------------------------------------------------------------------------

def test_meta_published_time_is_extracted():
    html = '<html><head><meta property="article:published_time" content="2026-10-20"></head></html>'
    assert extract_page_dates(html) == ["2026-10-20"]


def test_visible_data_date_is_extracted():
    html = "<body><p>数据口径日期：2026-07-29</p></body>"
    assert extract_page_dates(html) == ["2026-07-29"]


def test_page_without_date_returns_empty():
    """页面没写日期是事实，不该编一个。"""
    assert extract_page_dates("<body><p>个人版 49 元/月</p></body>") == []


def test_unrelated_dates_are_not_publication_dates():
    """页面上的任意日期不等于"这条内容是什么时候的"：合同日期不算发布日。"""
    html = "<body><p>合同有效期至 2024-01-31</p><p>价格 49 元/月</p></body>"
    assert extract_page_dates(html) == []


def test_extracted_value_is_iso_only():
    """抽出的值被**重写**成 ISO：带时刻的原文与带标签的原文都只剩年月日。

    页面内容是攻击者可控的，落进报告的只能是"年-月-日"三段数字。
    """
    html = ('<head><meta property="article:published_time" '
            'content="2026-10-20T08:00:00Z"></head>'
            '<body><p>数据口径日期：2026-10-20<script>alert(1)</script></p></body>')
    dates = extract_page_dates(html)
    assert dates == ["2026-10-20"]
    assert all("<" not in d and ">" not in d and "T" not in d for d in dates)


def test_invalid_date_is_dropped():
    assert extract_page_dates("<body><p>数据口径日期：2026-13-45</p></body>") == []


# --------------------------------------------------------------------------
# 2) 报告小节
# --------------------------------------------------------------------------

def _competitor():
    return {
        "name": "示例",
        "url": "https://demo.test/",
        "status": "success",
        "extracted_data": [
            {"key": "pricing", "value": "49 元/月", "source_url": "https://demo.test/"},
        ],
        "page_dates": [
            {"url": "https://demo.test/index.html", "dates": ["2001-02-03"]},
            {"url": "https://demo.test/pricing.html", "dates": []},
        ],
    }


def test_freshness_section_lists_source_dates():
    section = QualityScorer([_competitor()]).generate_quality_section()
    assert "信息时效" in section
    assert "2001-02-03" in section
    assert "https://demo.test/index.html" in section
    assert "未标注" in section  # 没写日期的页面如实标出，不留空


def test_freshness_section_has_no_crawl_time():
    """抓取时间不进报告：它只说明何时取回，不代表数据时点。"""
    section = QualityScorer([_competitor()]).generate_quality_section()
    assert date.today().isoformat() not in section


def test_no_section_when_no_page_dates():
    assert QualityScorer([{"name": "x", "url": "https://x.test/",
                           "extracted_data": []}])._freshness_section() == ""


# --------------------------------------------------------------------------
# 3) 语料档位覆盖（掉档守卫）
# --------------------------------------------------------------------------

def _fixture_pages():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "evals" / "cases"
    for path in sorted(root.rglob("*.html")):
        yield path


@pytest.mark.parametrize("band", ["fresh", "mid", "stale"])
def test_fixture_corpus_still_spans_three_freshness_bands(band):
    """语料必须仍横跨近/中/过时三档。

    夹具里的日期是固定值（相对日期会让报告每次都变、A/B 失去同样本前提），
    代价是语料会变旧：fresh 档跨过 90 天、mid 档跨过 180 天都会掉档。
    这条断言在语料变旧时**变红**，逼着人用 `evals/bake_fixture_dates.py
    --reference <新基准日>` 重烘焙，而不是让 freshness 维度静默退化成常量。
    """
    today = date.today()
    ages = []
    for path in _fixture_pages():
        for iso in extract_page_dates(path.read_text(encoding="utf-8")):
            ages.append((today - date.fromisoformat(iso)).days)
    if band == "fresh":
        assert any(0 <= age <= 90 for age in ages), "语料里没有近 90 天的页面"
    elif band == "mid":
        assert any(90 < age <= 180 for age in ages), "语料里没有 90~180 天的页面"
    else:
        assert any(age > 180 for age in ages), "语料里没有超过 180 天的页面"


def test_fixture_corpus_expiry_is_known():
    """重烘焙的触发点：按今天的语料推算，最早掉档的页面还有多少余量。"""
    today = date.today()
    ages = []
    for path in _fixture_pages():
        for iso in extract_page_dates(path.read_text(encoding="utf-8")):
            ages.append((today - date.fromisoformat(iso)).days)
    fresh_ages = [a for a in ages if 0 <= a <= 90]
    assert fresh_ages, "语料里没有近 90 天的页面"
    # 余量不足 30 天就该重烘焙了（提前提醒，不等它静默掉档）
    assert max(fresh_ages) <= 60, (
        f"fresh 档最旧的页面已经 {max(fresh_ages)} 天（余量不足 30 天），"
        f"该用 evals/bake_fixture_dates.py --reference 重烘焙了"
    )
