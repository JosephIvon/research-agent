"""准入闸门（抓后淘汰 + 未采信清单 + 从留池补位）的管线级行为。

对应文档：``docs/admission-metric-gap.md`` 第 4 节 (a)(b)。
"""

import asyncio

from src.workflow import mvp_workflow

import pytest


def _comp(name, url, dims):
    return {
        "name": name,
        "url": url,
        "status": "success",
        "extracted_data": [
            {"key": key, "value": f"{key} 的实际值"} for key in dims
        ],
    }


def _state(competitors, pool, min_count=3, rule="必须有官网 + 至少 2 个维度有数据"):
    return {
        "competitors": list(competitors),
        "rejected_competitors": [],
        "extracted_data": [],
        "raw_search_results": [],
        "target_sites": [],
        "target_urls": [],
        "discovery": {"scored": list(pool)},
        "research_plan": {
            "competitor_criteria": {
                "layers": ["direct"], "min_count": min_count, "max_count": 5,
                "admission_rule": rule,
            },
        },
    }


def _workflow():
    # 不注入 llm_client 之外的依赖：这一步不碰 LLM，也不碰网络
    return mvp_workflow.MVPWorkflow(llm_client=object())


def test_competitors_without_enough_dimensions_are_moved_to_the_unverified_list():
    state = _state(
        competitors=[
            _comp("猫箱", "https://maoxiangai.com/", ["pricing", "features"]),
            _comp("数据不足的", "https://thin.test/", ["pricing"]),
        ],
        pool=[],
    )
    workflow = _workflow()
    asyncio.run(workflow._apply_admission_gate(state))

    assert [c["name"] for c in state["competitors"]] == ["猫箱"]
    assert [r["name"] for r in state["rejected_competitors"]] == ["数据不足的"]
    rejected = state["rejected_competitors"][0]
    assert rejected["dimension_count"] == 1
    assert any("维度" in r for r in rejected["reasons"])
    assert state["admission_gate"]["rejected_count"] == 1


def test_backfill_takes_the_next_candidate_from_the_retained_pool():
    """被淘汰后从留池补位——留池就是 discovery.scored（按分排序）。"""
    state = _state(
        competitors=[
            _comp("猫箱", "https://maoxiangai.com/", ["pricing", "features"]),
            _comp("数据不足的", "https://thin.test/", ["pricing"]),
        ],
        pool=[
            {
                "name": "筑梦岛", "url": "https://www.zhumengdao.com/",
                "score": 0.5, "gate_passed": True, "site_type": "product_site",
            },
            {
                "name": "知乎专栏", "url": "https://zhuanlan.zhihu.com/p/1",
                "score": 0.5, "gate_passed": False, "site_type": "media",
            },
        ],
        min_count=2,
    )
    workflow = _workflow()

    async def _fake_fetch(sites, *args, **kwargs):
        return [
            _comp("筑梦岛", site["url"], ["pricing", "features", "models"])
            for site in sites
        ]

    workflow._fetch_all_competitors = _fake_fetch
    asyncio.run(workflow._apply_admission_gate(state))

    names = [c["name"] for c in state["competitors"]]
    assert names == ["猫箱", "筑梦岛"]
    assert state["admission_gate"]["backfilled"], "补位要留下记录"
    assert state["admission_gate"]["met_minimum"] is True


def test_backfill_never_picks_candidates_the_gate_rejected():
    """留池里没过闸门的候选不能被补位选中（媒体站顶上来等于把老问题换回来）。"""
    state = _state(
        competitors=[_comp("猫箱", "https://maoxiangai.com/", ["pricing"])],
        pool=[
            {
                "name": "知乎专栏", "url": "https://zhuanlan.zhihu.com/p/1",
                "score": 0.5, "gate_passed": False, "site_type": "media",
            },
        ],
    )
    workflow = _workflow()
    called = []

    async def _fake_fetch(sites, *args, **kwargs):
        called.append(sites)
        return []

    workflow._fetch_all_competitors = _fake_fetch
    asyncio.run(workflow._apply_admission_gate(state))

    assert called == [], "没过闸门的候选不该被抓"
    assert [c["name"] for c in state["competitors"]] == []
    assert state["admission_gate"]["met_minimum"] is False


def test_backfill_is_capped():
    """补位有上限（每家一次整站爬），不能无限补。"""
    pool = [
        {
            "name": f"候选{i}", "url": f"https://cand{i}.test/",
            "score": 0.5, "gate_passed": True, "site_type": "product_site",
        }
        for i in range(6)
    ]
    state = _state(
        competitors=[_comp("猫箱", "https://maoxiangai.com/", ["pricing"])],
        pool=pool,
        min_count=6,
    )
    workflow = _workflow()
    fetched = []

    async def _fake_fetch(sites, *args, **kwargs):
        fetched.extend(sites)
        return [
            _comp("补位的", site["url"], ["pricing", "features"])
            for site in sites
        ]

    workflow._fetch_all_competitors = _fake_fetch
    asyncio.run(workflow._apply_admission_gate(state))

    assert len(fetched) == mvp_workflow.MAX_BACKFILL_COMPETITORS
    assert state["admission_gate"]["met_minimum"] is False


def test_gate_records_the_executable_rule_for_the_report():
    """闸门要把"实际执行的规则"记进 state——报告要能说清按什么口径选的。"""
    state = _state(
        competitors=[_comp("猫箱", "https://maoxiangai.com/", ["pricing", "features"])],
        pool=[],
        rule="必须有官网 + 至少 2 个维度有数据",
    )
    workflow = _workflow()
    asyncio.run(workflow._apply_admission_gate(state))
    rule = state["admission_gate"]["rule"]
    assert rule["require_product_site"] is True
    assert rule["min_dimensions"] == 2
    assert rule["min_dimensions_stage"] == "post_crawl"
    assert rule["executable"]
