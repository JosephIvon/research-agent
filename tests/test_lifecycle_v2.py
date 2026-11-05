"""生命周期 v2（D7/M2–M4）验收：阶段包装 / 终态单调 / 检查点 / 续跑 / 单元超时。

零网络：复用 test_scripted_llm 的离线 crawler + ScriptedLLM。
崩溃注入用“阶段检查点落盘后抛异常”模拟 kill -9（进程真死的验收证据见
docs/design/2026-11-04-track.md 的 resume 探针一节）。
"""
import asyncio
import functools
import json
from typing import Any, Dict, List

import pytest

from src.agent.citations import EvidenceStore
from src.llm import ScriptedLLMClient
from src.storage import database as dbmod
from src.workflow.mvp_workflow import MVPWorkflow
from src.workflow.task_store import (
    InMemoryTaskStore,
    TaskEventType,
    TaskStatus,
    is_terminal,
)
from tests.test_scripted_llm import EXTRACT_ITEMS, TARGET_URL, _FakeWebCrawler


class _Collector:
    def __init__(self):
        self.events: List[Any] = []

    async def emit(self, event):
        self.events.append(event)

    def names(self):
        return [e.event for e in self.events]


class _Crash(Exception):
    pass


class _CrashAfterStage(MVPWorkflow):
    """在指定阶段检查点落盘后立刻抛异常，模拟该阶段的进程猝死。"""

    def __init__(self, crash_stage: str, **kwargs):
        self._crash_stage = crash_stage
        super().__init__(**kwargs)

    def _save_checkpoint(self, state, stage):
        super()._save_checkpoint(state, stage)
        if stage == self._crash_stage:
            raise _Crash(stage)


# --------------------------------------------------------------------------
# B2：终态单调
# --------------------------------------------------------------------------

def test_terminal_status_cannot_regress():
    store = InMemoryTaskStore()
    asyncio.run(store.create("t1", "q", {"report": True, "prd": False}))
    asyncio.run(store.update("t1", status=TaskStatus.CANCELLED))
    asyncio.run(store.update("t1", status=TaskStatus.RUNNING))

    task = asyncio.run(store.get("t1"))
    assert task.status == TaskStatus.CANCELLED
    assert is_terminal(task.status)
    assert is_terminal("completed") and not is_terminal("running")


def test_cancelled_is_a_declared_terminal_status():
    assert TaskStatus.CANCELLED.value == "cancelled"
    assert TaskEventType.TASK_CANCELLED.value == "task_cancelled"


def test_cancel_event_stops_workflow_before_side_effects(tmp_path, monkeypatch):
    """取消后不得再落报告：raise_if_cancelled 在 _save_report 之前生效。"""
    client, cite, report = _scripted_client()
    build = _wire(monkeypatch, tmp_path, client)
    workflow = build()
    cancel_event = asyncio.Event()
    cancel_event.set()

    state = asyncio.run(workflow.run(
        "Acme 定价调研",
        target_urls=[TARGET_URL],
        enable_search=False,
        deliverables={"report": True, "prd": False},
        task_id="cancel-before-save",
        cancel_event=cancel_event,
    ))

    assert state["status"] == "cancelled"
    assert not list((tmp_path / "output").glob("research_report_*.md")), \
        "取消后不得产生报告文件"


# --------------------------------------------------------------------------
# B3：检查点与事件日志
# --------------------------------------------------------------------------

def test_checkpoint_and_event_seq_survive_new_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)
    db = dbmod.get_report_db()

    seq1 = db.append_task_event("t1", {"event": "stage_start", "stage": "crawl"})
    seq2 = db.append_task_event("t1", {"event": "stage_end", "stage": "crawl"})
    assert seq1 and seq2 and seq2 > seq1, "同任务事件 seq 必须单调递增"

    db.save_checkpoint("t1", "crawl", {"stage": "crawl", "user_query": "q"}, 1)
    db.save_checkpoint("t1", "verify", {"stage": "verify", "user_query": "q"}, 1)

    # 新连接/新进程视角：seq 与检查点都还在
    monkeypatch.setattr(dbmod, "_report_db", None)
    db2 = dbmod.get_report_db()
    events = db2.list_task_events("t1")
    assert [e["seq"] for e in events] == [seq1, seq2]
    latest = db2.load_latest_checkpoint("t1")
    assert latest["stage"] == "verify"
    # 两张表各自有独立 AUTOINCREMENT；各自单调即可，不能跨表比大小。
    assert latest["seq"] > 0


def test_stage_wrapper_records_attempt_and_checkpoint():
    saved = []
    collector = _Collector()
    workflow = MVPWorkflow(event_emitter=collector)
    workflow._save_checkpoint = lambda state, stage: saved.append(stage)

    state = {"task_id": "t1"}

    async def _drive():
        workflow._begin_stage(state, "crawl", "开始抓取")
        workflow._end_stage(state, "crawl", "completed", "抓取完成")
        workflow._begin_stage(state, "crawl", "重试抓取")
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(_drive())

    names = collector.names()
    assert names[:2] == [TaskEventType.STAGE_START.value, TaskEventType.STAGE_END.value]
    assert collector.events[0].payload["attempt"] == 1
    assert collector.events[1].payload["attempt"] == 1
    assert collector.events[2].payload["attempt"] == 2
    assert saved == ["crawl"]


def test_resume_flags_skip_completed_stages():
    workflow = MVPWorkflow()
    workflow._emit = lambda *a, **k: None
    state = {}
    workflow._apply_resume(state, {"seq": 7, "stage": "verify", "payload": {"stage": "verify"}})

    assert state["_resume_past_decompose"] is True
    assert state["_resume_past_crawl"] is True
    assert state["_resume_past_extract"] is True
    assert state["_resume_past_verify"] is True
    assert state["_resume_past_report"] is False
    assert state["_resume_past_citation"] is False


# --------------------------------------------------------------------------
# B4：单元级 wait_for
# --------------------------------------------------------------------------

def test_unit_fetch_timeout_isolated_to_one_site(monkeypatch):
    workflow = MVPWorkflow(llm_client=object(), request_interval=0)
    monkeypatch.setenv("UNIT_FETCH_TIMEOUT", "0.05")

    async def _slow(url, *a, **k):
        await asyncio.sleep(1)
        return {"url": url, "status": "success", "extracted_data": []}

    monkeypatch.setattr(workflow, "_fetch_single_competitor", _slow)
    # 抓取前的限速睡睡由并发控制器控制；这里直接跳过批次爬虫初始化。

    results = asyncio.run(workflow._fetch_all_competitors([{"url": "https://slow.test"}]))
    assert len(results) == 1
    assert results[0]["status"] == "failed"
    assert "超时" in results[0]["error_message"]


# --------------------------------------------------------------------------
# B3：崩溃注入 + 续跑（每个阶段都从自己的检查点恢复）
# --------------------------------------------------------------------------

def _scripted_client():
    expected = [dict(item, source_url=TARGET_URL) for item in EXTRACT_ITEMS]
    store = EvidenceStore.from_extracted_data(expected)
    cite = store.evidences[0].citation
    report = f"## 定价\n\n标准版 $49/席位/月 {cite}，企业版 $99/席位/月 {cite}。\n"
    return ScriptedLLMClient(script={
        "extract": [json.dumps(EXTRACT_ITEMS, ensure_ascii=False)],
        "report": [report],
    }), cite, report


def _wire(monkeypatch, tmp_path, client, crawler_cls=_FakeWebCrawler):
    """工作流层零网络隔离：LLM、爬虫、URL 校验走构造注入。

    返回绑好替身的构造器；调用时可用关键字覆盖（如 `build(event_emitter=...)`）。
    """
    monkeypatch.setattr("src.config.settings.OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)
    return functools.partial(
        MVPWorkflow,
        llm_client=client,
        crawler_factory=crawler_cls,
        url_validator=lambda u: u,
    )


def test_sqlite_read_fallback_after_restart(tmp_path, monkeypatch):
    """重启后内存 store 为空：历史任务应从 SQLite 回填；内存命中时以内存为准。"""
    import server as server_module

    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)
    db = dbmod.get_report_db()
    db.save_task("hist-1", "历史任务", "completed", report_id="rep-hist",
                 deliverables={"report": True, "prd": False})

    store = InMemoryTaskStore()
    monkeypatch.setattr(server_module, "get_task_store", lambda: store)

    task, from_sqlite = asyncio.run(server_module._load_task_for_read("hist-1"))
    assert from_sqlite is True
    assert task.status == TaskStatus.COMPLETED
    assert task.user_query == "历史任务"
    assert task.report_id == "rep-hist"

    asyncio.run(store.create("live-1", "实时任务", {"report": True, "prd": False}))
    asyncio.run(store.update("live-1", status=TaskStatus.RUNNING))
    # 即使 SQLite 有同 id 的行，内存命中时不得回退
    db.save_task("live-1", "实时任务", "failed")
    live, live_from_sqlite = asyncio.run(server_module._load_task_for_read("live-1"))
    assert live_from_sqlite is False
    assert live.status == TaskStatus.RUNNING


def _report_rows(tmp_path, task_id: str) -> int:
    import sqlite3

    path = tmp_path / "reports.db"
    if not path.exists():
        return 0
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM reports WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_report_write_is_idempotent_when_crash_after_write(tmp_path, monkeypatch):
    """“文件已写、状态未落库”窗口被杀：续跑只覆盖同一文件，不产生第二份。"""
    client, cite, report = _scripted_client()
    build = _wire(monkeypatch, tmp_path, client)
    task_id = "atomic-report-1"

    class _CrashAfterWrite(MVPWorkflow):
        def _persist_to_sqlite(self, state):
            raise _Crash("after report write")

    first = asyncio.run(_CrashAfterWrite(**build.keywords).run(
        "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id=task_id,
    ))
    assert first["status"] == "failed", "注入的异常必须让首跑失败"
    files_at_crash = list((tmp_path / "output").glob("research_report_*.md"))
    assert len(files_at_crash) == 1, "崩溃时文件应已写盘"
    assert _report_rows(tmp_path, task_id) == 0, "此时台账还没有行"

    # 崩溃与重启在现实中不会发生在同一秒；拉开一秒，确保“旧命名”变异会真的产生新文件名。
    import time

    time.sleep(1.05)

    resumed = build()
    state = asyncio.run(resumed.run(
        "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id=task_id, resume=True,
    ))
    assert state["status"] == "completed", state.get("termination_reason")
    files = list((tmp_path / "output").glob("research_report_*.md"))
    assert len(files) == 1, "同一任务续跑后磁盘只能有一份报告"
    assert files[0].name == files_at_crash[0].name, "续跑必须复用同一文件名"
    assert cite in files[0].read_text(encoding="utf-8")
    assert _report_rows(tmp_path, task_id) == 1, "磁盘文件数必须等于台账行数"
    assert state["report_id"] == files[0].stem


def test_report_draft_reused_after_stage_internal_crash(tmp_path, monkeypatch):
    """报告已生成、检查点未落盘时崩溃：续跑复用草稿，报告生成不再调用 LLM。"""
    client, cite, report = _scripted_client()
    # 草稿失效时需要第二次生成仍可完成，避免测试因脚本耗尽而“假红”。
    client = ScriptedLLMClient(script={
        "extract": [json.dumps(EXTRACT_ITEMS, ensure_ascii=False)],
        "report": [report, report],
    })
    build = _wire(monkeypatch, tmp_path, client)
    task_id = "draft-report-1"

    class _CrashBeforeReportCheckpoint(MVPWorkflow):
        def _end_stage(self, state, stage, status="completed", message="", error_code=None):
            if stage == "report" and status == "completed":
                raise _Crash("report before checkpoint")
            super()._end_stage(state, stage, status, message, error_code)

    first = asyncio.run(_CrashBeforeReportCheckpoint(**build.keywords).run(
        "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id=task_id,
    ))
    assert first["status"] == "failed"
    assert dbmod.get_report_db().load_latest_checkpoint(task_id)["stage"] == "verify"

    resumed = build()
    calls = {"generate": 0}
    original = resumed.copywriter.generate_report

    async def _counted(*args, **kwargs):
        calls["generate"] += 1
        return await original(*args, **kwargs)

    resumed.copywriter.generate_report = _counted
    state = asyncio.run(resumed.run(
        "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id=task_id, resume=True,
    ))
    assert state["status"] == "completed", state.get("termination_reason")
    assert calls["generate"] == 0, "草稿命中时不得重复生成报告"
    # 草稿是模型原文；机器附录是确定性渲染，续跑后会重新拼接，因此只校验主体一致。
    assert report.strip() in state["report_final"], "续跑报告主体必须与首跑产物一致"


def test_crawl_page_cache_skips_refetch_on_stage_internal_crash(tmp_path, monkeypatch):
    """抓取完成、检查点未落盘时崩溃：续跑复用页缓存，外部请求增量为 0。"""
    client, cite, report = _scripted_client()
    # 抓取阶段重跑会再抽取一次，脚本给两份，避免因脚本耗尽误判为抽取失败。
    client = ScriptedLLMClient(script={
        "extract": [
            json.dumps(EXTRACT_ITEMS, ensure_ascii=False),
            json.dumps(EXTRACT_ITEMS, ensure_ascii=False),
        ],
        "report": [report],
    })
    calls = {"fetch": 0}

    class _CountingCrawler(_FakeWebCrawler):
        async def fetch_site_profile(self, url, **kwargs):
            calls["fetch"] += 1
            return await super().fetch_site_profile(url, **kwargs)

    build = _wire(monkeypatch, tmp_path, client, crawler_cls=_CountingCrawler)
    task_id = "cache-crawl-1"

    class _CrashBeforeCrawlCheckpoint(MVPWorkflow):
        def _end_stage(self, state, stage, status="completed", message="", error_code=None):
            if stage == "crawl" and status == "completed":
                raise _Crash("crawl before checkpoint")
            super()._end_stage(state, stage, status, message, error_code)

    first = asyncio.run(_CrashBeforeCrawlCheckpoint(**build.keywords).run(
        "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id=task_id,
    ))
    assert first["status"] == "failed"
    assert calls["fetch"] == 1, "首跑只应抓取一次"
    assert dbmod.get_report_db().load_latest_checkpoint(task_id)["stage"] == "decompose"

    before = calls["fetch"]
    state = asyncio.run(build().run(
        "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id=task_id, resume=True,
    ))
    assert state["status"] == "completed", state.get("termination_reason")
    assert calls["fetch"] - before == 0, "页缓存命中时续跑不得再发抓取请求"


@pytest.mark.parametrize("crash_stage", ["crawl", "extract", "verify", "report", "citation"])
def test_resume_after_crash_reaches_completed(tmp_path, monkeypatch, crash_stage):
    client, cite, _ = _scripted_client()
    build = _wire(monkeypatch, tmp_path, client)
    task_id = f"crash-{crash_stage}"

    crashed = _CrashAfterStage(
        crash_stage=crash_stage, event_emitter=_Collector(), **build.keywords,
    )
    bad_state = asyncio.run(crashed.run(
        "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id=task_id,
    ))
    assert bad_state["status"] == "failed", "注入的异常必须让首跑失败"
    checkpoint = dbmod.get_report_db().load_latest_checkpoint(task_id)
    assert checkpoint and checkpoint["stage"] == crash_stage

    resumed = build(event_emitter=_Collector())
    state = asyncio.run(resumed.run(
        "Acme 定价调研", target_urls=[TARGET_URL], enable_search=False,
        deliverables={"report": True, "prd": False}, task_id=task_id, resume=True,
    ))
    assert state["status"] == "completed", state.get("termination_reason")
    assert state["_resume_stage"] == crash_stage
    assert cite in state["report_final"]


# --------------------------------------------------------------------------
# 历史任务读取：重启后内存 store 为空时，SSE / cancel / resume 不得 404
# --------------------------------------------------------------------------

def _seed_history(tmp_path, monkeypatch, task_id: str, status: str = "running"):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)
    db = dbmod.get_report_db()
    db.save_task(
        task_id, "历史任务", status,
        deliverables={"report": True, "prd": False}, user_id="test-user",
    )
    return db


def test_history_sse_replays_sqlite_events(tmp_path, monkeypatch, auth_headers):
    """历史任务的 SSE 必须 200，并按 SQLite seq 回放事件后关流（不挂死）。"""
    import server as server_module
    from fastapi.testclient import TestClient

    task_id = "hist-sse"
    db = _seed_history(tmp_path, monkeypatch, task_id, status="completed")
    seq1 = db.append_task_event(task_id, {
        "event": "stage_start", "stage": "crawl", "status": "running",
        "message": "开始抓取", "payload": {},
    })
    seq2 = db.append_task_event(task_id, {
        "event": "stage_end", "stage": "crawl", "status": "completed",
        "message": "抓取完成", "payload": {},
    })
    assert seq1 and seq2

    store = InMemoryTaskStore()
    monkeypatch.setattr(server_module, "get_task_store", lambda: store)

    client = TestClient(server_module.app)
    r = client.get(f"/research/tasks/{task_id}/events", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.text
    assert "stage_start" in body and "stage_end" in body
    assert f"id: {seq1}" in body and f"id: {seq2}" in body


def test_history_cancel_writes_terminal_status_to_sqlite(tmp_path, monkeypatch, auth_headers):
    """重启后取消历史任务：没有 asyncio 任务可取消，但 SQLite 必须落 cancelled。"""
    import server as server_module
    from fastapi.testclient import TestClient

    task_id = "hist-cancel"
    db = _seed_history(tmp_path, monkeypatch, task_id, status="running")
    store = InMemoryTaskStore()
    monkeypatch.setattr(server_module, "get_task_store", lambda: store)

    client = TestClient(server_module.app)
    r = client.post(f"/research/tasks/{task_id}/cancel", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] is True
    assert db.get_task(task_id)["status"] == "cancelled"
    assert any(
        e["event"] == TaskEventType.TASK_CANCELLED.value
        for e in db.list_task_events(task_id)
    )


def test_history_resume_rebuilds_memory_task_and_audits(tmp_path, monkeypatch, auth_headers):
    """重启后续跑历史任务：先重建内存 TaskState，再续跑；不得 404 / 静默 no-op。"""
    import server as server_module
    from fastapi.testclient import TestClient

    task_id = "hist-resume"
    db = _seed_history(tmp_path, monkeypatch, task_id, status="running")
    db.save_checkpoint(task_id, "verify", {"stage": "verify", "user_query": "历史任务"}, 1)
    store = InMemoryTaskStore()
    monkeypatch.setattr(server_module, "get_task_store", lambda: store)
    # 只验端点行为，不真的拉起工作流（那会打真实网络）
    monkeypatch.setattr(server_module, "_schedule_resume", lambda *a, **k: None)

    client = TestClient(server_module.app)
    r = client.post(f"/research/tasks/{task_id}/resume", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["resumed"] is True
    rebuilt = asyncio.run(store.get(task_id))
    assert rebuilt is not None, "续跑前必须把历史任务重建进内存 store"
    assert any(
        e["event"] == TaskEventType.TASK_RESUMED.value
        for e in db.list_task_events(task_id)
    )
