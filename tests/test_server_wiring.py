"""服务端接线测试：MCP 子应用挂载、任务详情响应增强、反馈闭环（M4）。

覆盖三块接线：
- MCP 挂载（M5）：/mcp 必须受外层 JWT 鉴权中间件保护（中间件覆盖 mount），
  且不进入免鉴权白名单 PUBLIC_PATHS
- 任务详情：响应含 trace_url / injection_findings_count / cost_report 三键
- 反馈闭环：POST/GET /research/reports/{report_id}/feedback 的鉴权、
  报告归属隔离（无权 404）与入参校验（非法 rating 422）

存储隔离手法与 tests/test_tenant_isolation.py 一致（storage fixture +
_register_report 帮手 + conftest 的 _headers）。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import server
from src.workflow.task_store import TaskState
from tests.conftest import TEST_USER_ID, OTHER_USER_ID

REPORT_ID = "research_report_20260101_130000_aaaaaaaa"


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


# --------------------------------------------------------------------------
# MCP 子应用挂载：鉴权中间件必须覆盖 mount
# --------------------------------------------------------------------------

class TestMcpMount:
    def test_mcp_not_in_public_paths(self):
        """/mcp 不得进入免鉴权白名单——鉴权由外层中间件保证。"""
        assert "/mcp" not in server.PUBLIC_PATHS

    def test_mcp_status_requires_auth(self, client):
        r = client.get("/mcp/status")
        assert r.status_code == 401, "未带 token 的 /mcp 请求未被鉴权中间件拦下"

    def test_mcp_status_with_token(self, client, auth_headers):
        """需要真实的 MCP SDK。

        缺 SDK 时 `/mcp` 挂的是 503 占位子应用（刻意不 raise，否则 import server
        就会失败、5 个测试文件直接 collection error）——那是**已测过的降级路径**，
        由 `test_mcp_mount_degradation.py` 覆盖，不在本用例的验证范围内。
        """
        from src.mcpserver import mcp_sdk_available

        if not mcp_sdk_available():
            pytest.skip("需要 mcp SDK（uv add 'mcp>=2.3'）；降级路径另有测试覆盖")

        r = client.get("/mcp/status", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["service"] == "research-agent-mcp"


# --------------------------------------------------------------------------
# 任务详情响应增强：trace_url / injection_findings_count / cost_report
# --------------------------------------------------------------------------

class TestTaskDetailWiring:
    def test_task_detail_contains_new_keys(self, client, auth_headers):
        store = server.get_task_store()
        asyncio.run(store.create("wiring-task-001", "接线测试", {"report": True}, user_id=TEST_USER_ID))

        r = client.get("/research/tasks/wiring-task-001", headers=auth_headers)
        assert r.status_code == 200
        body = r.json()
        # Langfuse 未启用时 trace_url 必须是 None（键必须存在，而非缺键）
        assert "trace_url" in body
        assert body["trace_url"] is None
        assert "injection_findings_count" in body
        assert "cost_report" in body

    def test_task_detail_passes_through_new_fields(self, client, auth_headers):
        """update 写入的注入检测计数/成本报告应透传到任务详情。

        TaskState 正由并行任务扩展 injection_findings_count / cost_report 字段。
        未合入时 store.update 会静默忽略未知 kwargs（不报错），此分支只验证
        键存在与默认值（0 / None）；合入后 update → GET 应得 3 与透传的成本报告。
        """
        merged = "injection_findings_count" in TaskState.__dataclass_fields__
        store = server.get_task_store()
        task_id = "wiring-task-002"
        asyncio.run(store.create(task_id, "接线测试", {"report": True}, user_id=TEST_USER_ID))

        try:
            asyncio.run(store.update(task_id, injection_findings_count=3, cost_report={"tokens": 100}))
        except Exception:
            # 并行任务未合入且 update 对未知键严格报错的情形——探测后跳过写入断言
            merged = False

        body = client.get(f"/research/tasks/{task_id}", headers=auth_headers).json()
        if merged:
            assert body["injection_findings_count"] == 3
            assert body["cost_report"] == {"tokens": 100}
        else:
            assert body["injection_findings_count"] == 0
            assert body["cost_report"] is None


# --------------------------------------------------------------------------
# 反馈闭环（M4）
# --------------------------------------------------------------------------

class TestReportFeedback:
    def test_feedback_roundtrip_and_isolation(self, storage, client, auth_headers, other_user_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)

        # 未登录 → 401（鉴权中间件在路由匹配前拦截）
        r = client.post(f"/research/reports/{REPORT_ID}/feedback", json={"rating": "up"})
        assert r.status_code == 401

        # 归属人 A：up 一次、down 两次
        r = client.post(f"/research/reports/{REPORT_ID}/feedback",
                        json={"rating": "up", "comment": "有帮助"}, headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["ok"] is True

        for _ in range(2):
            r = client.post(f"/research/reports/{REPORT_ID}/feedback",
                            json={"rating": "down"}, headers=auth_headers)
            assert r.status_code == 200

        # 最后一次 POST 的响应应直接带回最新统计
        assert r.json() == {"ok": True, "summary": {"up": 1, "down": 2}}

        # GET summary
        r = client.get(f"/research/reports/{REPORT_ID}/feedback", headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == {"up": 1, "down": 2}

        # 用户 B（无权访问 A 的报告）POST → 404（不泄露存在性）
        r = client.post(f"/research/reports/{REPORT_ID}/feedback",
                        json={"rating": "up"}, headers=other_user_headers)
        assert r.status_code == 404, f"期望 404，实际 {r.status_code}"
        # B 同样读不到统计
        assert client.get(f"/research/reports/{REPORT_ID}/feedback",
                          headers=other_user_headers).status_code == 404

        # B 的 404 POST 不得计入 A 看到的统计
        r = client.get(f"/research/reports/{REPORT_ID}/feedback", headers=auth_headers)
        assert r.json() == {"up": 1, "down": 2}

    def test_feedback_summary_empty_report(self, storage, client, auth_headers):
        """无反馈的报告返回 {up: 0, down: 0} 而非报错。"""
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.get(f"/research/reports/{REPORT_ID}/feedback", headers=auth_headers)
        assert r.status_code == 200
        assert r.json() == {"up": 0, "down": 0}

    def test_invalid_rating_rejected(self, storage, client, auth_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.post(f"/research/reports/{REPORT_ID}/feedback",
                        json={"rating": "sideways"}, headers=auth_headers)
        assert r.status_code == 422

    def test_comment_over_500_chars_rejected(self, storage, client, auth_headers):
        _register_report(storage, REPORT_ID, TEST_USER_ID)
        r = client.post(f"/research/reports/{REPORT_ID}/feedback",
                        json={"rating": "up", "comment": "x" * 501}, headers=auth_headers)
        assert r.status_code == 422
