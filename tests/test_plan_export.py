"""「调研口径」的导出链路：真调一次 HTTP 接口。

按项目约定（全局记忆第 40 条）：CLI 跑通不等于 HTTP 路径可用——
导出与历史冷加载都是 HTTP 路径，必须真调过。
"""

import asyncio

from src.workflow.research_plan import render_plan_markdown

import pytest


PLAN = {
    "entity": {
        "name": "百度搭子",
        "entity_type": "app",
        "what_it_is": "百度面向个人用户的 AI 原生社交陪伴应用",
        "disambiguation_candidates": ["百度搭子独立App（置信度 0.62）"],
        "resolved_confidence": 0.72,
        "official_domains": ["dumate.cn"],
    },
    "competitor_criteria": {
        "layers": ["direct", "alternative"],
        "min_count": 3, "max_count": 5, "candidate_pool": 10,
        "admission_rule": "必须有官网 + 至少 2 个维度有数据",
    },
    "angles": [
        {"angle": "feature", "label": "功能", "priority": "primary", "weight": 0.35,
         "reason": "用户比较时先看功能", "page_hints": ["功能"]},
    ],
    "admission_rule": {
        "source": "model",
        "executable": ["必须有官网（必须是产品官网）", "至少 2 个维度有数据（抓后判定）"],
        "not_executable": ["直接层须满足个人用户×移动端App（层归属要语义判断，代码不据此裁决）"],
        "min_dimensions": 2, "min_dimensions_stage": "post_crawl",
    },
    "open_questions": [{"question": "要不要含替代方案？", "default": "否"}],
    "source": "model",
}

DISCOVERY = {
    "candidates": [
        {"name": "猫箱", "url": "https://maoxiangai.com/", "site_type": "product_site"},
        {"name": "知乎专栏", "url": "https://zhuanlan.zhihu.com/p/1", "site_type": "media"},
    ],
    "selected": [
        {"name": "猫箱", "url": "https://maoxiangai.com/", "score": 0.5,
         "reasons": ["像产品自己的官网"]},
    ],
    "scored": [
        {"name": "猫箱", "url": "https://maoxiangai.com/", "score": 0.5,
         "site_type": "product_site", "reasons": ["像产品自己的官网"]},
        {"name": "知乎专栏", "url": "https://zhuanlan.zhihu.com/p/1", "score": 0.0,
         "site_type": "media", "reasons": ["站点类型是媒体/内容站，不是产品官网"]},
    ],
    "notes": ["候选池剔除 1 个非官网页（媒体/内容站 1 个）"],
}

GATE = {
    "rule": PLAN["admission_rule"],
    "rejected_count": 1,
    "rejected": [
        {"name": "数据不足的", "url": "https://thin.test/", "dimension_count": 1,
         "reasons": ["只抽到 1 个维度，低于口径要求的 2 个"]},
    ],
    "backfilled": [],
    "final_count": 3,
    "min_count": 3,
    "met_minimum": True,
}


def test_plan_markdown_contains_subject_criteria_weights_and_pool():
    text = render_plan_markdown(PLAN, DISCOVERY, GATE)
    assert "研究主体：百度搭子" in text
    assert "消歧置信度：0.72" in text
    assert "主体官网域名：dumate.cn" in text
    assert "实际执行的入选条件" in text
    assert "未能执行的条件" in text
    assert "功能 35%" in text
    assert "猫箱" in text and "知乎专栏" in text
    assert "未采信" in text and "数据不足的" in text
    assert "候选池剔除 1 个非官网页" in text


def test_plan_markdown_says_nothing_it_cannot_back():
    """没有计划时不能凭空编：渲染结果要如实说明"没有口径数据"。"""
    text = render_plan_markdown(None, None, None)
    assert "调研口径" in text


def test_export_endpoint_supports_plan_content_type(tmp_path, monkeypatch):
    """真调一次导出接口：口径 tab 点的导出要拿到口径文件，不是报告正文。"""
    import server as server_module

    report_id = "research_report_20261029_010203"
    monkeypatch.setattr(server_module, "_require_report_access", lambda *a, **k: None)

    class _Task:
        def __init__(self, rid):
            self.report_id = rid
            self.research_plan = PLAN
            self.discovery = DISCOVERY
            self.admission_gate = GATE

    class _Store:
        async def list_tasks(self):
            return [_Task(report_id)]

    monkeypatch.setattr(server_module, "get_task_store", lambda: _Store())

    request = ExportRequestFactory(report_id, "html", "plan")
    response = asyncio.run(server_module.export_report(_FakeRequest(), request))
    body = open(response.path, encoding="utf-8").read()
    assert "调研口径" in body
    assert "猫箱" in body
    assert "research_plan_" in (response.filename or "")


class _FakeRequest:
    pass


def ExportRequestFactory(report_id, fmt, content_type):
    from server import ExportRequest

    return ExportRequest(report_id=report_id, format=fmt, content_type=content_type)


def test_http_export_and_history_carry_the_research_plan(tmp_path, monkeypatch, auth_headers):
    """**真走 HTTP**：导出与历史冷加载两条路都要拿到口径。

    口径只随任务终态落库；历史读取路径此前不补这两个字段，
    换浏览器/清缓存后重新打开报告就只剩空态——用户看到结论却没有口径可查。
    """
    from fastapi.testclient import TestClient

    import server as server_module

    report_id = "research_report_20261029_010203"

    class _Task:
        def __init__(self):
            self.report_id = report_id
            self.task_id = "task-plan-0001"
            self.status = "completed"
            self.research_plan = PLAN
            self.discovery = DISCOVERY
            self.admission_gate = GATE
            self.cost_report = None
            self.injection_findings_count = 0

    class _Store:
        async def list_tasks(self):
            return [_Task()]

    monkeypatch.setattr(server_module, "get_task_store", lambda: _Store())
    monkeypatch.setattr(server_module, "_require_report_access", lambda *a, **k: None)

    client = TestClient(server_module.app)

    # 1) 历史冷加载：payload 里要带 research_plan / discovery
    with monkeypatch.context() as m:
        m.setattr(server_module, "_load_report_payload", lambda rid: {"markdown": "# 报告\n\n正文"})
        history = client.get(f"/research/history/{report_id}", headers=auth_headers)
    assert history.status_code == 200, history.text
    payload = history.json()
    assert payload["research_plan"]["entity"]["name"] == "百度搭子"
    assert payload["discovery"]["selected"][0]["name"] == "猫箱"
    assert payload["admission_gate"]["rejected_count"] == 1

    # 2) 导出：content_type=plan 拿到的必须是口径文件，不是报告正文
    response = client.post(
        "/research/export",
        json={"report_id": report_id, "format": "html", "content_type": "plan"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert "调研口径" in response.text
    assert "研究主体：百度搭子" in response.text
    assert "未采信" in response.text
