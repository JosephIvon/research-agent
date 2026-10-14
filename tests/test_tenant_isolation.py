"""跨用户数据隔离测试。

改造前 `reports` / `tasks` 两张表都没有归属列，`GET /research/history` 返回全量
报告，任意登录用户可读可删他人交付物。本文件锁死修复后的行为。

隔离策略：
- 有归属的报告/任务 → 仅归属人可读，admin 放行
- 无主（user_id 为 NULL）或库中无归属记录的文件 → **仅 admin 可见**
- 无权访问一律返 404 而非 403，避免泄露"该资源存在"
- 版本链按 (user_id, query_hash) 隔离，防止跨用户串链
"""
import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server
from src.storage import REPORT_NOT_IN_DB
from src.workflow.task_store import TaskStatus
from tests.conftest import TEST_USER_ID, OTHER_USER_ID, _headers

REPORT_ID = "research_report_20260101_120000_aaaaaaaa"
OTHER_REPORT_ID = "research_report_20260101_120001_bbbbbbbb"


class _FakeRequest:
    """最小 Request 替身：守卫函数只读 request.state 上的 user_id / role。"""

    def __init__(self, user_id=None, role="user"):
        self.state = SimpleNamespace(user_id=user_id, role=role)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """把数据库与输出目录隔离到 tmp_path，避免污染真实数据。"""
    from src.auth import user_db as usermod
    from src.storage import database as dbmod

    monkeypatch.setenv("USER_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setattr(usermod, "_user_db", None)
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path / "output"))

    db = dbmod.get_report_db()
    try:
        yield db
    finally:
        monkeypatch.setattr(dbmod, "_report_db", None)
        monkeypatch.setattr(usermod, "_user_db", None)


def _register_report(db, report_id: str, owner, content: str = "# 竞品报告\n\n内容") -> str:
    """同时写入文件与数据库，模拟正常产出的报告。"""
    out = server._output_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{report_id}.md").write_text(content, encoding="utf-8")
    db.save_report(report_id, "task-x", "查询", report_content=content, user_id=owner)
    return report_id


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def real_admin_headers(storage):
    """库中真实存在的管理员 + 其 token，用于特权写端点测试。

    _require_admin 查库比对，token 里自称 admin 但库里不是的（见下方
    伪造/降权用例）一律 403。
    """
    from src.auth.user_db import get_user_db

    db = get_user_db()
    user_id = db.create_user(
        "real-admin", "real-admin@test.local", "Adm1n-Passw0rd-2026", role="admin"
    )
    assert user_id, "测试管理员创建失败"
    return _headers(user_id, role="admin")


class TestReportIsolation:
    def test_owner_can_read_own_report(self, storage, client, auth_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.get(f"/research/history/{REPORT_ID}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["markdown"].startswith("# 竞品报告")

    def test_other_user_gets_404_not_403(self, storage, client, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.get(f"/research/history/{REPORT_ID}", headers=other_user_headers)
        # 必须是 404：403 会告诉对方"这份报告存在"
        assert r.status_code == 404, f"期望 404，实际 {r.status_code}"
        assert r.json()["detail"] == "报告不存在"

    def test_history_list_excludes_other_users_reports(self, storage, client, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.get("/research/history", headers=other_user_headers)
        assert r.status_code == 200
        assert all(item["id"] != REPORT_ID for item in r.json()), "他人报告出现在列表中"

    def test_other_user_cannot_delete(self, storage, client, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.delete(f"/research/history/{REPORT_ID}", headers=other_user_headers)
        assert r.status_code == 404
        # 文件必须还在
        assert (server._output_dir() / f"{REPORT_ID}.md").exists(), "报告文件被越权删除了"

    def test_owner_can_delete_and_db_row_is_removed(self, storage, client, auth_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.delete(f"/research/history/{REPORT_ID}", headers=auth_headers)
        assert r.status_code == 200
        assert not (server._output_dir() / f"{REPORT_ID}.md").exists()
        assert storage.get_report(REPORT_ID) is None, "数据库行仍在，报告会继续出现在历史列表"

    def test_admin_can_read_across_users(self, storage, client, admin_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.get(f"/research/history/{REPORT_ID}", headers=admin_headers)
        assert r.status_code == 200


class TestOrphanReports:
    """无主报告：存量数据或库中查不到归属记录的文件。"""

    def test_orphan_report_is_admin_only(self, storage, client, auth_headers, other_user_headers):
        # 只写文件、不写库 → 归属未知
        out = server._output_dir()
        out.mkdir(parents=True, exist_ok=True)
        (out / "research_report_20260101_120000_deadbeef.md").write_text("# 孤儿", encoding="utf-8")

        assert client.get("/research/history/research_report_20260101_120000_deadbeef",
                          headers=auth_headers).status_code == 404
        assert client.get("/research/history/research_report_20260101_120000_deadbeef",
                          headers=other_user_headers).status_code == 404
        assert client.get("/research/history/research_report_20260101_120000_deadbeef",
                          headers=client_admin_headers()).status_code == 200

    def test_null_owner_row_is_admin_only(self, storage, client, auth_headers):
        rid = _register_report(storage, REPORT_ID, None)  # user_id 为 NULL
        assert client.get(f"/research/history/{rid}", headers=auth_headers).status_code == 404

    def test_sentinel_distinguishes_absent_from_null_owner(self, storage):
        _register_report(storage, REPORT_ID, None)
        assert storage.get_report_owner(REPORT_ID) is None
        assert storage.get_report_owner("research_report_20991231_235959_ffffffff") is REPORT_NOT_IN_DB


def client_admin_headers():
    from src.auth.jwt_auth import create_access_token
    token = create_access_token({"sub": "admin-user", "username": "admin", "role": "admin"})
    return {"Authorization": f"Bearer {token}"}


class TestTaskIsolation:
    @pytest.fixture(autouse=True)
    def _fresh_store(self):
        store = server.get_task_store()
        yield
        asyncio.run(store.update("tenant-task-a", status=TaskStatus.COMPLETED))

    def _make_task(self, task_id: str, owner):
        store = server.get_task_store()
        asyncio.run(store.create(task_id, "查询", {"report": True, "prd": False}, user_id=owner))

    def test_owner_can_read_own_task(self, client, auth_headers):
        self._make_task("tenant-task-a", TEST_USER_ID)
        r = client.get("/research/tasks/tenant-task-a", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["user_id"] == TEST_USER_ID

    def test_other_user_cannot_read_task(self, client, other_user_headers):
        self._make_task("tenant-task-a", TEST_USER_ID)
        r = client.get("/research/tasks/tenant-task-a", headers=other_user_headers)
        assert r.status_code == 404, f"期望 404，实际 {r.status_code}"

    def test_other_user_cannot_subscribe_to_sse(self, client, other_user_headers):
        """SSE 端点的守卫以单元测试覆盖。

        刻意**不**通过 TestClient 打这个端点：一旦访问校验失效，SSE 会无限流下去，
        而 Starlette TestClient 的 ASGI transport 会缓冲整个响应，表现为**挂死**
        而不是失败——CI 上会卡到超时。这里直接验证守卫函数，效果等价且确定。
        """
        self._make_task("tenant-task-a", TEST_USER_ID)
        task = asyncio.run(server.get_task_store().get("tenant-task-a"))

        with pytest.raises(HTTPException) as exc:
            server._require_task_access(task, _FakeRequest(OTHER_USER_ID, "user"))
        assert exc.value.status_code == 404

        # 端点确实调用了这个守卫（避免守卫存在却没被接线）
        source = inspect.getsource(server.stream_task_events)
        assert "_require_task_access" in source, "SSE 端点未接入归属校验"

    def test_admin_can_read_task(self, client, admin_headers):
        self._make_task("tenant-task-a", TEST_USER_ID)
        assert client.get("/research/tasks/tenant-task-a", headers=admin_headers).status_code == 200

    def test_legacy_task_without_owner_is_admin_only(self, client, auth_headers):
        store = server.get_task_store()
        asyncio.run(store.create("tenant-legacy-task", "旧任务", {"report": True, "prd": False}))
        assert client.get("/research/tasks/tenant-legacy-task", headers=auth_headers).status_code == 404


class TestSyncIsolation:
    """文档同步端点同样存在越权面：不传 report_id 时会取"磁盘上最新的报告"，
    而那是全局的，任意用户都能把别人的报告同步到飞书/腾讯。
    """

    @pytest.fixture(autouse=True)
    def _feishu_configured(self, monkeypatch):
        monkeypatch.setattr(server, "FEISHU_APP_ID", "cli_test")
        monkeypatch.setattr(server, "FEISHU_APP_SECRET", "secret")

    def test_sync_without_report_id_does_not_pick_other_users_report(self, storage, client, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.post("/sync/feishu", json={}, headers=other_user_headers)
        assert r.status_code == 404, f"同步端点泄露了他人报告，实际 {r.status_code}"

    def test_sync_with_other_users_report_id_is_rejected(self, storage, client, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.post("/sync/feishu", json={"report_id": REPORT_ID}, headers=other_user_headers)
        assert r.status_code == 404, f"同步端点泄露了他人报告，实际 {r.status_code}"

    def test_sync_tencent_same_guards(self, storage, client, other_user_headers, monkeypatch):
        monkeypatch.setattr(server, "TENCENT_APP_ID", "cli_test")
        monkeypatch.setattr(server, "TENCENT_APP_SECRET", "secret")
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.post("/sync/tencent", json={"report_id": REPORT_ID}, headers=other_user_headers)
        assert r.status_code == 404

    def test_tencent_sync_without_report_id_does_not_leak(self, storage, client, other_user_headers, monkeypatch):
        monkeypatch.setattr(server, "TENCENT_APP_ID", "cli_test")
        monkeypatch.setattr(server, "TENCENT_APP_SECRET", "secret")
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        assert client.post("/sync/tencent", json={}, headers=other_user_headers).status_code == 404


class TestExportAndCompareIsolation:
    def test_other_user_cannot_export(self, storage, client, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.post("/research/export",
                        json={"report_id": REPORT_ID, "format": "html"},
                        headers=other_user_headers)
        assert r.status_code == 404

    def test_other_user_cannot_compare_against_others_report(self, storage, client, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        _register_report(storage, OTHER_REPORT_ID, OTHER_USER_ID)
        r = client.get(f"/research/compare/{REPORT_ID}/{OTHER_REPORT_ID}",
                       headers=other_user_headers)
        assert r.status_code == 404

    def test_other_user_cannot_list_versions(self, storage, client, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.get(f"/research/versions/{REPORT_ID}", headers=other_user_headers)
        assert r.status_code == 404


class TestReportIdCompatibility:
    def test_new_and_legacy_formats_both_accepted(self):
        pattern = server.REPORT_ID_RE
        assert pattern.fullmatch("research_report_20260517_120000")          # 历史格式
        assert pattern.fullmatch("research_report_20260517_120000_a1b2c3d4")  # 新格式
        assert not pattern.fullmatch("research_report_20260517_120000_zz")   # 非 hex
        assert not pattern.fullmatch("../secrets")                            # 穿越
        assert not pattern.fullmatch("research_report_20260517")              # 缺段


class TestFilenameCollision:
    def test_same_second_different_tasks_do_not_collide(self, storage):
        from src.workflow.mvp_workflow import MVPWorkflow

        wf = MVPWorkflow.__new__(MVPWorkflow)  # 不跑 __init__（避免建立 LLM 客户端）
        p1 = wf._save_report("A", "aaaaaaaa-1111")
        p2 = wf._save_report("B", "bbbbbbbb-2222")

        assert p1 != p2, "同一秒内的两个任务产生了相同文件名，会互相覆盖"
        assert server.REPORT_ID_RE.fullmatch(server.Path(p1).stem)
        assert server.REPORT_ID_RE.fullmatch(server.Path(p2).stem)


class TestVersionChainIsolation:
    """版本链必须按用户隔离。

    必须端到端验证生产代码 `_persist_to_sqlite`：如果测试里自己重算一遍 hash
    再断言两个 hash 不同，那是在测测试自己，不构成任何保护。
    """

    @staticmethod
    def _state(task_id, report_id, user_id, query="同一个问题"):
        return {
            "user_query": query,
            "task_id": task_id,
            "report_id": report_id,
            "user_id": user_id,
            "quality_score": 7.0,
            "quality_grade": "B",
            "competitors": [],
            "missing_dimensions": [],
            "verification_score": 7.0,
            "status": "completed",
        }

    def test_same_query_different_users_do_not_chain(self, storage):
        from src.workflow.mvp_workflow import MVPWorkflow

        wf = MVPWorkflow.__new__(MVPWorkflow)  # 只调用 _persist_to_sqlite，无需完整初始化

        wf._persist_to_sqlite(self._state("t-a1", "rep_a1", TEST_USER_ID))
        wf._persist_to_sqlite(self._state("t-b1", "rep_b1", OTHER_USER_ID))

        row_b = storage.get_report("rep_b1")
        row_a = storage.get_report("rep_a1")
        assert row_b["parent_report_id"] is None, "B 的报告被挂到了 A 的版本链上"
        assert row_b["query_hash"] != row_a["query_hash"], "版本链键未按用户隔离"
        assert row_b["user_id"] == OTHER_USER_ID

    def test_same_user_still_gets_version_chain(self, storage):
        """隔离不能把版本链功能本身弄坏：同一用户的第二次调研仍应串成新版本。"""
        from src.workflow.mvp_workflow import MVPWorkflow

        wf = MVPWorkflow.__new__(MVPWorkflow)
        wf._persist_to_sqlite(self._state("t-a1", "rep_a1", TEST_USER_ID))
        wf._persist_to_sqlite(self._state("t-a2", "rep_a2", TEST_USER_ID))

        row_a2 = storage.get_report("rep_a2")
        assert row_a2["parent_report_id"] == "rep_a1", "同一用户的新版本没有挂到旧版本上"
        assert row_a2["version_number"] == 2


class TestSettingsRequireAdmin:
    """_require_admin 以数据库为准，不信 token 的 role claim。"""

    def test_non_admin_cannot_change_runtime_settings(self, storage, client, auth_headers):
        r = client.post("/settings/RATE_LIMIT_PER_TOKEN", params={"value": "999/minute"},
                        headers=auth_headers)
        assert r.status_code == 403, f"普通用户可改运行时配置，实际 {r.status_code}"

    def test_admin_can_change_runtime_settings(self, storage, client, real_admin_headers):
        r = client.post("/settings/RATE_LIMIT_PER_TOKEN", params={"value": "130/minute"},
                        headers=real_admin_headers)
        assert r.status_code == 200

    def test_admin_claim_without_db_record_is_rejected(self, storage, client, admin_headers):
        """token 自称 admin 但库里查无此人（伪造 claim / 库被重建）→ 403。"""
        r = client.post("/settings/RATE_LIMIT_PER_TOKEN", params={"value": "130/minute"},
                        headers=admin_headers)
        assert r.status_code == 403, "JWT role claim 未查库即放行"

    def test_admin_demoted_in_db_loses_write_access_immediately(self, storage, client, real_admin_headers):
        """降权即时生效：token 仍带 admin，但库里已改为 user → 403。

        这就是 _require_admin 查库比对的意义——将来加入改角色入口后，
        旧 token 在有效期内不得继续保有管理员写权限。
        """
        from src.auth import user_db as usermod

        with usermod._connect() as conn:
            conn.execute("UPDATE users SET role = 'user' WHERE username = 'real-admin'")
            conn.commit()

        r = client.post("/settings/RATE_LIMIT_PER_TOKEN", params={"value": "130/minute"},
                        headers=real_admin_headers)
        assert r.status_code == 403, "降权后旧 token 仍可改配置"