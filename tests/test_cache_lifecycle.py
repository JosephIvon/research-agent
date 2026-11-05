"""缓存生命周期清理验收：终态年龄、容量双限、干跑、清理后仍可续跑。

零网络；续跑沿用 test_lifecycle_v2 的离线夹具。
"""
import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.storage import database as dbmod
from src.workflow.mvp_workflow import MVPWorkflow
from tests.test_scripted_llm import TARGET_URL


@pytest.fixture()
def cache_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "cache.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)
    dbmod.init_db()
    return dbmod.get_report_db(), tmp_path / "cache.db"


def _backdate(path, task_id: str, age_days: float):
    old = (datetime.now() - timedelta(days=age_days)).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (old, task_id))
        conn.commit()
    finally:
        conn.close()


def _seed(db, path, task_id: str, status: str, age_days: float,
          pages: int = 2, drafts: int = 2, size: int = 200):
    db.save_task(task_id, f"q-{task_id}", status)
    for i in range(pages):
        db.save_page_cache(
            task_id, f"https://{task_id}/{i}", "a" * 64,
            {"pages": [{"url": f"https://{task_id}/{i}", "content": "x" * size}]},
        )
    for i in range(drafts):
        db.save_task_draft(task_id, f"draft-{i}", "y" * size)
    _backdate(path, task_id, age_days)


def _rows_for(path, task_id: str):
    conn = sqlite3.connect(str(path))
    try:
        pages = conn.execute(
            "SELECT COUNT(*) FROM page_cache WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        drafts = conn.execute(
            "SELECT COUNT(*) FROM task_drafts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
        return pages + drafts
    finally:
        conn.close()


def test_terminal_age_prune_never_touches_running(cache_db):
    db, path = cache_db
    _seed(db, path, "done-old", "completed", 10)
    _seed(db, path, "cancel-old", "cancelled", 10)
    _seed(db, path, "failed-old", "failed", 40)
    _seed(db, path, "failed-new", "failed", 10)
    _seed(db, path, "running-old", "running", 100)

    summary = db.prune_caches(
        dry_run=False, completed_keep_days=7, failed_keep_days=30,
        max_rows=10 ** 6, max_bytes=10 ** 9,
    )

    assert summary["deleted"]["total"]["rows"] == 12, "3 个过期终态任务各 4 行"
    assert _rows_for(path, "done-old") == 0
    assert _rows_for(path, "cancel-old") == 0
    assert _rows_for(path, "failed-old") == 0
    assert _rows_for(path, "failed-new") == 4, "failed 在 30 天窗口内必须保留"
    assert _rows_for(path, "running-old") == 4, "running 任务永不清理"
    assert summary["caps"]["exceeded_after"] is False


def test_capacity_row_cap_evicts_oldest_terminal_only(cache_db):
    db, path = cache_db
    _seed(db, path, "run", "running", 50, pages=2, drafts=2)
    _seed(db, path, "old", "completed", 3)
    _seed(db, path, "mid", "completed", 2)
    _seed(db, path, "new", "completed", 1)

    summary = db.prune_caches(
        dry_run=False, completed_keep_days=7, failed_keep_days=30,
        max_rows=8, max_bytes=10 ** 9,
    )

    assert _rows_for(path, "run") == 4, "running 不计入淘汰"
    assert _rows_for(path, "old") == 0
    assert _rows_for(path, "mid") == 0
    assert _rows_for(path, "new") == 4, "最旧优先淘汰，保留最新"
    assert summary["remaining"]["rows"] == 8
    assert summary["caps"]["exceeded_after"] is False


def test_capacity_byte_cap_and_running_can_remain_over_cap(cache_db):
    db, path = cache_db
    _seed(db, path, "run", "running", 50, pages=2, drafts=2, size=500)
    _seed(db, path, "old", "completed", 3, size=500)
    _seed(db, path, "new", "completed", 1, size=500)

    conn = sqlite3.connect(str(path))
    try:
        run_bytes = conn.execute("""
            SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0) FROM page_cache
            WHERE task_id = 'run'
        """).fetchone()[0] + conn.execute("""
            SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0) FROM task_drafts
            WHERE task_id = 'run'
        """).fetchone()[0]
        new_bytes = conn.execute("""
            SELECT COALESCE(SUM(length(CAST(payload_json AS BLOB))), 0) FROM page_cache
            WHERE task_id = 'new'
        """).fetchone()[0] + conn.execute("""
            SELECT COALESCE(SUM(length(CAST(content AS BLOB))), 0) FROM task_drafts
            WHERE task_id = 'new'
        """).fetchone()[0]
    finally:
        conn.close()

    summary = db.prune_caches(
        dry_run=False, completed_keep_days=7, failed_keep_days=30,
        max_rows=10 ** 6, max_bytes=run_bytes + new_bytes + 1,
    )

    assert _rows_for(path, "old") == 0, "字节超限时仍按最旧优先"
    assert _rows_for(path, "new") == 4
    assert _rows_for(path, "run") == 4

    # 只有 running 数据时不可能再降：必须如实报告超限，而不是误删 running。
    summary2 = db.prune_caches(
        dry_run=False, completed_keep_days=7, failed_keep_days=30,
        max_rows=10 ** 6, max_bytes=0,
    )
    assert _rows_for(path, "run") == 4
    assert summary2["deleted"]["total"]["rows"] == 4, "只剩 new 可删"
    assert summary2["caps"]["exceeded_after"] is True


def test_dry_run_reports_without_deleting(cache_db):
    db, path = cache_db
    _seed(db, path, "done-old", "completed", 10)
    _seed(db, path, "run", "running", 100)

    summary = db.prune_caches(
        dry_run=True, completed_keep_days=7, failed_keep_days=30,
        max_rows=10 ** 6, max_bytes=10 ** 9,
    )

    assert summary["dry_run"] is True
    assert summary["deleted"]["total"]["rows"] == 4
    assert summary["deleted"]["total"]["bytes"] > 0
    assert _rows_for(path, "done-old") == 4, "干跑不得删除"
    assert _rows_for(path, "run") == 4


def test_prune_keeps_checkpoints_and_resume_still_works(tmp_path, monkeypatch, cache_db):
    from tests.test_lifecycle_v2 import _CrashAfterStage, _scripted_client, _wire

    db, path = cache_db
    client, cite, _ = _scripted_client()
    build = _wire(monkeypatch, tmp_path, client)
    # _wire 会把库切到 tmp/reports.db；以它为准，否则检查点落在另一个文件。
    dbmod.init_db()
    db = dbmod.get_report_db()
    path = tmp_path / "reports.db"

    crash_task = "prune-resume-1"
    asyncio.run(_CrashAfterStage(crash_stage="verify", **build.keywords).run(
        "Acme 定价调研", target_urls=[TARGET_URL],
        enable_search=False, deliverables={"report": True, "prd": False},
        task_id=crash_task,
    ))
    assert db.load_latest_checkpoint(crash_task)["stage"] == "verify"
    _seed(db, path, "done-old", "completed", 10)
    db.save_page_cache(crash_task, "https://acme.test/1", "b" * 64,
                       {"pages": [{"url": "https://acme.test/1", "content": "cached"}]})

    summary = db.prune_caches(
        dry_run=False, completed_keep_days=7, failed_keep_days=30,
        max_rows=10 ** 6, max_bytes=10 ** 9,
    )
    assert _rows_for(path, "done-old") == 0
    assert summary["deleted"]["total"]["rows"] >= 4

    # 清理只碰缓存表：检查点/事件原样保留，续跑仍能完成。
    assert db.load_latest_checkpoint(crash_task)["stage"] == "verify"
    resumed = asyncio.run(build().run(
        "Acme 定价调研", target_urls=[TARGET_URL],
        enable_search=False, deliverables={"report": True, "prd": False},
        task_id=crash_task, resume=True,
    ))
    assert resumed["status"] == "completed", resumed.get("termination_reason")
    assert cite in resumed["report_final"]
