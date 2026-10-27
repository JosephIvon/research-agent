"""页级失败可见性：爬虫记下的 failed_pages 必须走到竞品条目，并外发成事件。

背景：爬虫早就把「发现出来但没抓到」的页面记在 `profile["failed_pages"]` 里
（url / 异常类型 / 耗时），但这个字段此前**只停在爬虫的返回值里**——
`mvp_workflow` 只读了 content / pages / discovered_urls，于是：

- 报告的「来源」页看不到少了几页，用户无从查证；
- 过程记录里也没有任何痕迹，只剩服务端日志。

这组用例钉住两件事：竞品条目带上 failed_pages，且每个失败页都外发一条
crawl_progress(failed) 事件（前端过程记录按 status 渲染成红色条目）。
"""
import asyncio

import pytest

from src.llm import ScriptedLLMClient
from src.workflow.mvp_workflow import MVPWorkflow


class _Collector:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class _FakeCrawler:
    """只实现 fetch_site_profile——返回带 failed_pages 的画像。"""

    def __init__(self, profile):
        self._profile = profile

    async def fetch_site_profile(self, url, **kwargs):
        return dict(self._profile)


def _profile(failed_pages):
    return {
        "content": "Acme 定价 每月 99 美元，包含 10 个席位。",
        "pages": [{"url": "https://acme.com", "content": "Acme 定价 每月 99 美元"}],
        "discovered_urls": ["https://acme.com/pricing", "https://acme.com/about"],
        "failed_pages": failed_pages,
    }


def _workflow(monkeypatch, collector):
    from src.workflow import mvp_workflow

    monkeypatch.setattr(
        mvp_workflow,
        "get_llm_client",
        lambda *a, **k: ScriptedLLMClient(script={"react": ["占位，本用例不真调模型"]}),
    )
    wf = MVPWorkflow(event_emitter=collector)

    async def _fake_extract(content, url):
        return []

    wf.coordinator.extract_key_info = _fake_extract
    return wf


def test_failed_pages_land_in_the_competitor_entry(monkeypatch):
    """竞品条目必须带上 failed_pages——否则报告页无从显示「少了几页」。"""
    collector = _Collector()
    wf = _workflow(monkeypatch, collector)
    failed = [
        {"url": "https://acme.com/pricing", "error": "TimeoutError",
         "elapsed_seconds": 15.2, "detail": "timeout 15000ms exceeded"},
        {"url": "https://acme.com/about", "error": "ERR_CONNECTION_CLOSED",
         "elapsed_seconds": 0.8, "detail": "connection closed"},
    ]

    result = asyncio.run(
        wf._fetch_single_competitor(
            "https://acme.com", crawler=_FakeCrawler(_profile(failed))
        )
    )

    assert result["status"] == "success"
    assert result["failed_pages"] == failed, "页级失败明细必须原样带到竞品条目"
    # 抓到的页面不受影响：失败页是补充信息，不是把整站判死
    assert result["raw_pages"], "成功抓到的页不能因为别页失败而丢"


def test_every_failed_page_emits_a_visible_event(monkeypatch):
    """每个失败页都要外发一条事件——过程记录里得看得到红字。"""
    collector = _Collector()
    wf = _workflow(monkeypatch, collector)
    failed = [
        {"url": "https://acme.com/pricing", "error": "TimeoutError",
         "elapsed_seconds": 15.2, "detail": ""},
    ]

    asyncio.run(
        wf._fetch_single_competitor(
            "https://acme.com", crawler=_FakeCrawler(_profile(failed))
        )
    )

    failed_events = [e for e in collector.events if getattr(e, "status", "") == "failed"]
    assert len(failed_events) == 1, f"应有 1 条失败事件，实得 {len(failed_events)}"
    event = failed_events[0]
    assert event.event == "crawl_progress", "复用已登记的事件名，不新增事件类型"
    assert event.stage == "crawl", "失败必须落在抓取这一步"
    assert "https://acme.com/pricing" in event.message, "事件文案要带上是哪个页面"
    assert "TimeoutError" in event.message, "事件文案要带上失败原因"


def test_a_clean_run_emits_no_failure_event(monkeypatch):
    """没有失败页时不能凭空发失败事件——否则红字就成了噪声。"""
    collector = _Collector()
    wf = _workflow(monkeypatch, collector)

    result = asyncio.run(
        wf._fetch_single_competitor(
            "https://acme.com", crawler=_FakeCrawler(_profile([]))
        )
    )

    assert result["failed_pages"] == []
    assert not [e for e in collector.events if getattr(e, "status", "") == "failed"]


def test_site_level_failure_still_carries_the_field(monkeypatch):
    """整站失败时条目里也要有 failed_pages 这个键——形状必须一致。"""
    collector = _Collector()
    wf = _workflow(monkeypatch, collector)

    class _Boom:
        async def fetch_site_profile(self, url, **kwargs):
            raise RuntimeError("整站挂了")

    result = asyncio.run(
        wf._fetch_single_competitor("https://acme.com", crawler=_Boom())
    )

    assert result["status"] == "failed"
    assert result["failed_pages"] == []
