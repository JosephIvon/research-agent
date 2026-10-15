"""抓取失败可见性 + 不可重试失败分类（`fetch_site_profile` / `_fetch_page_snapshot`）。

这两条的由来是 8 站 × 30 页 × 3 遍（714 次）真实抓取实测里暴露的两个盲区：

1. **失败页被静默吞掉**。原实现是 `except Exception: continue`——URL 与原因
   一起消失，生产日志里只剩「少了几页」这个事实，事后无法区分超时、
   被切断连接、还是该 URL 压根不是页面。
2. **重试对所有异常一视同仁**。`Download is starting` 意味着该 URL 返回文件
   而不是页面，重试在定义上产不出 HTML。

**不覆盖 `ERR_CONNECTION_CLOSED`**：实测 43 次全部来自 10s 超时的探针，
换成生产 60s 后只剩 2/30，样本不足以支持「不重试」（见 §5.7 的 P4）。
因此专门加一条守卫，把这个「暂时不处理」钉死，免得以后被顺手改掉。
"""
import asyncio
import time

import pytest

from src.crawler.web_crawler import WebCrawler, _is_non_retryable


class _Boom(Exception):
    pass


class _FakeCrawler(WebCrawler):
    """只替换抓取与发现，其余走真实实现——要测的正是那两处。"""

    def __init__(self, fail_for=(), sleep=0.0):
        self.calls = []
        self._fail_for = set(fail_for)
        self._sleep = sleep

    async def _fetch_page_snapshot(self, url, **kwargs):
        self.calls.append(url)
        if self._sleep:
            time.sleep(self._sleep)
        if url in self._fail_for:
            raise _Boom(f"boom: {url}")
        return {"url": url, "content": f"正文 {url}", "html": "<html></html>"}

    def discover_research_links(self, url, html, max_links=0, **kwargs):
        return [f"{url.rstrip('/')}/a", f"{url.rstrip('/')}/b", f"{url.rstrip('/')}/c"]


# --------------------------------------------------------------------------
# P1：失败页必须留痕
# --------------------------------------------------------------------------

def test_failed_page_records_url_error_and_elapsed():
    """失败页要带 URL、异常类型、耗时——这三样缺一不可，否则事后无从归因。

    耗时必须真的来自实测（`_FakeCrawler` 里让失败分支睡 0.05s）：
    断言写成 `>= 0` 的话，写死成常量 0 也能过——那这条守卫就是假的。
    """
    c = _FakeCrawler(fail_for=["https://acme.com/b"], sleep=0.05)

    prof = asyncio.run(c.fetch_site_profile("https://acme.com"))

    assert [f["url"] for f in prof["failed_pages"]] == ["https://acme.com/b"]
    got = prof["failed_pages"][0]
    assert got["error"] == "_Boom", got
    assert isinstance(got["elapsed_seconds"], float), got
    assert got["elapsed_seconds"] >= 0.04, (
        f"耗时必须是实测值而不是常量：{got}"
    )
    assert "boom" in got["detail"], got


def test_success_run_reports_empty_failed_pages():
    """全成功时也要有该键且为空列表——调用方才能无条件读它。"""
    prof = asyncio.run(_FakeCrawler().fetch_site_profile("https://acme.com"))

    assert prof["failed_pages"] == []
    assert len(prof["pages"]) == 4, "首页 + 3 个发现页"


def test_failure_does_not_stop_remaining_pages():
    """一页失败不能连坐后面的页——原实现是 continue，这里钉住。"""
    c = _FakeCrawler(fail_for=["https://acme.com/a", "https://acme.com/c"])

    prof = asyncio.run(c.fetch_site_profile("https://acme.com"))

    assert len(prof["failed_pages"]) == 2
    assert [p["url"] for p in prof["pages"]] == [
        "https://acme.com", "https://acme.com/b",
    ], "失败页之后仍要继续抓"


def test_every_discovered_url_is_either_page_or_failure():
    """不变量：发现数 == 成功数 + 失败数。缺口就是静默丢失。"""
    c = _FakeCrawler(fail_for=["https://acme.com/b"])

    prof = asyncio.run(c.fetch_site_profile("https://acme.com"))

    assert len(prof["discovered_urls"]) == (
        len(prof["pages"]) - 1 + len(prof["failed_pages"])
    )


# --------------------------------------------------------------------------
# P3：重试不可能成功的失败类型
# --------------------------------------------------------------------------

def test_download_is_never_retried():
    """`Download is starting` = 该 URL 返回文件，重试产不出 HTML。"""
    assert _is_non_retryable(RuntimeError("Page.goto: Download is starting")) is True


def test_connection_closed_is_still_retried():
    """刻意**不**纳入不可重试：证据不足以支持，且会误伤真实的临时 TCP reset。

    这是 §5.7 的 P4：43 次观测全部来自 10s 超时的探针，生产 60s 下只剩 2/30。
    在拿到生产日志的分类统计之前，保持现状并把这条钉死。
    """
    assert _is_non_retryable(
        RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED")
    ) is False
    assert _is_non_retryable(RuntimeError("Page.goto: Timeout 10000ms exceeded")) is False
    assert _is_non_retryable(RuntimeError("任意别的错")) is False


def test_non_retryable_skips_retry_loop():
    """真跑一遍重试循环：不可重试的异常只尝试一次。"""
    attempts = {"n": 0}

    class _Browser:
        async def new_page(self):
            attempts["n"] += 1
            raise RuntimeError("Page.goto: Download is starting")

    c = WebCrawler.__new__(WebCrawler)
    c.browser = _Browser()
    c.request_interval = 0

    with pytest.raises(RuntimeError):
        asyncio.run(c._fetch_page_snapshot("https://acme.com", max_attempts=3))

    assert attempts["n"] == 1, f"应只尝试一次，实际 {attempts['n']} 次"


def test_retryable_error_still_retries():
    """对照组：可重试的异常仍然走满 3 次——不能为了省事把重试整体关掉。"""
    attempts = {"n": 0}

    class _Browser:
        async def new_page(self):
            attempts["n"] += 1
            raise RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED")

    c = WebCrawler.__new__(WebCrawler)
    c.browser = _Browser()
    c.request_interval = 0

    async def _no_sleep(_):
        return None

    real_sleep = asyncio.sleep
    asyncio.sleep = _no_sleep
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(c._fetch_page_snapshot("https://acme.com", max_attempts=3))
    finally:
        asyncio.sleep = real_sleep

    assert attempts["n"] == 3, f"应重试满 3 次，实际 {attempts['n']} 次"
