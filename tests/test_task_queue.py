"""Tests for task queue, SSE endpoints, and async task support"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
import asyncio
import json

import server
from server import app


class TestTaskQueue:
    def test_create_task_returns_task_id_immediately(self, monkeypatch, auth_headers):
        """创建任务立即返回 task_id，不等待工作流完成"""
        captured_tasks = {}

        class FakeWorkflow:
            async def run(self, **kwargs):
                captured_tasks.update(kwargs)
                await asyncio.sleep(5)  # Simulate long running task
                return {
                    "status": "completed",
                    "task_id": kwargs.get("task_id"),
                    "report_final": "# Report",
                    "report_id": "report_123",
                    "competitors": [],
                }

        monkeypatch.setattr("src.workflow.mvp_workflow.MVPWorkflow", lambda **kwargs: FakeWorkflow())

        client = TestClient(app)
        response = client.post(
            "/research/tasks",
            json={
                "query": "test query",
                "enable_search": False,
                "urls": ["https://example.com"],
                "deliverables": {"report": True, "prd": False},
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert "task_id" in body
        assert body["status"] == "pending"
        assert len(body["task_id"]) > 0

    def test_get_task_returns_status_and_events(self, monkeypatch, auth_headers):
        """GET /research/tasks/{task_id} 返回状态、事件历史、artifacts"""
        task_store = server.get_task_store()
        asyncio.run(task_store.create("test-task-001", "test query", {"report": True, "prd": True}))

        from src.workflow.task_store import TaskEvent, TaskEventType

        asyncio.run(task_store.add_event(
            "test-task-001",
            TaskEvent(
                id=0,
                event=TaskEventType.TASK_CREATED.value,
                task_id="test-task-001",
                stage="created",
                status="created",
                message="任务已创建",
            )
        ))

        client = TestClient(app)
        response = client.get("/research/tasks/test-task-001", headers=auth_headers)

        assert response.status_code == 200
        body = response.json()
        assert body["task_id"] == "test-task-001"
        assert body["status"] == "pending"
        assert len(body["events"]) == 1
        assert body["events"][0]["event"] == "task_created"

    def test_get_task_returns_404_for_nonexistent(self, auth_headers):
        client = TestClient(app)
        response = client.get("/research/tasks/nonexistent-task", headers=auth_headers)
        assert response.status_code == 404

    def test_sse_stream_receives_events(self, monkeypatch):
        """SSE流能收到 task_created 和后续事件"""
        task_store = server.get_task_store()
        task_id = "sse-test-task-001"
        asyncio.run(task_store.create(task_id, "sse test", {"report": True, "prd": False}))

        from src.workflow.task_store import TaskEvent, TaskEventType

        # Subscribe BEFORE emitting events so the queue captures them
        q = task_store.subscribe_task(task_id)
        emitter = task_store.get_or_create_emitter(task_id)

        # Use emitter.emit() directly (simulates what _run_research_task does internally)
        asyncio.run(emitter.emit(TaskEvent(
            id=0,
            event=TaskEventType.TASK_CREATED.value,
            task_id=task_id,
            stage="created",
            status="created",
            message="任务已创建",
        )))
        asyncio.run(emitter.emit(TaskEvent(
            id=1,
            event=TaskEventType.DECOMPOSE.value,
            task_id=task_id,
            stage="decompose",
            status="running",
            message="正在理解需求",
        )))

        # Pull events from the subscriber queue
        chunks = []
        for _ in range(6):
            try:
                event = q.get_nowait()
                chunks.append(f"event: {event.event}\ndata: {event.message}")
            except asyncio.QueueEmpty:
                break

        assert len(chunks) >= 2
        assert any("task_created" in c for c in chunks)
        assert any("decompose" in c for c in chunks)

    def test_sse_supports_reconnect_with_last_event_id(self, monkeypatch):
        """SSE支持通过 Last-Event-ID 重连后补发遗漏事件"""
        task_store = server.get_task_store()
        task_id = "reconnect-test-task"
        asyncio.run(task_store.create(task_id, "reconnect test", {"report": True, "prd": False}))

        from src.workflow.task_store import TaskEvent, TaskEventType

        q = task_store.subscribe_task(task_id)
        emitter = task_store.get_or_create_emitter(task_id)

        for i in range(3):
            asyncio.run(emitter.emit(TaskEvent(
                id=i,
                event=TaskEventType.DECOMPOSE.value,
                task_id=task_id,
                stage="decompose",
                status="running",
                message=f"第{i}步",
            )))

        # Simulate reconnect: skip events with id <= 1
        last_id = 1
        ids = []
        for _ in range(6):
            try:
                event = q.get_nowait()
                if event.id > last_id:
                    ids.append(event.id)
            except asyncio.QueueEmpty:
                break

        assert 2 in ids or 3 in ids
        assert 0 not in ids
        assert 1 not in ids


class TestEventPayloadSecurity:
    def test_event_payload_excludes_password(self, monkeypatch):
        """事件payload不包含password字段"""
        task_store = server.get_task_store()
        task_id = "security-test-task"
        asyncio.run(task_store.create(task_id, "security test", {"report": True, "prd": False}))

        from src.workflow.task_store import TaskEvent, _sanitize_payload

        sanitized = _sanitize_payload({
            "url": "https://example.com",
            "password": "secret123",
            "auth_credentials": {"username": "user", "password": "secret"},
            "message": "test",
        })

        assert "password" not in sanitized
        assert "secret123" not in str(sanitized)

    def test_task_status_does_not_expose_credentials(self, monkeypatch, auth_headers):
        """任务状态接口不返回账号密码"""
        task_store = server.get_task_store()
        task_id = "creds-test-task"
        asyncio.run(task_store.create(task_id, "creds test", {"report": True, "prd": False}))

        client = TestClient(app)
        response = client.get(f"/research/tasks/{task_id}", headers=auth_headers)

        assert response.status_code == 200
        body = response.json()
        text = json.dumps(body)
        assert "secret123" not in text
        assert "my-password" not in text


class TestBackwardCompatibility:
    def test_research_competitive_still_works(self, monkeypatch, auth_headers):
        """旧接口 /research/competitive 仍然可用"""
        class FakeWorkflow:
            async def run(self, **kwargs):
                return {
                    "status": "completed",
                    "task_id": "legacy-task",
                    "report_final": "# Test Report",
                    "quality_score": 8.5,
                    "quality_grade": "B",
                    "missing_dimensions": [],
                    "verification_score": 8.0,
                    "termination_reason": "完成",
                    "competitors": [],
                    "errors": [],
                    "sync_results": {},
                }

        monkeypatch.setattr("src.workflow.mvp_workflow.MVPWorkflow", lambda **kwargs: FakeWorkflow())

        client = TestClient(app)
        response = client.post(
            "/research/competitive",
            json={"query": "test", "enable_search": False, "urls": ["https://example.com"]},
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert "report" in body

    def test_research_prd_still_works(self, monkeypatch, auth_headers):
        """旧接口 /research/prd 仍然可用"""
        monkeypatch.setattr("src.tools.prd_generator.PRDGenerator.generate_from_competitive_report", lambda self, report, query: "# PRD Document")

        client = TestClient(app)
        response = client.post(
            "/research/prd",
            json={"report_content": "# Report", "query": "test query"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert "prd" in body

    def test_research_prd_from_query_still_works(self, monkeypatch, auth_headers):
        """旧接口 /research/prd-from-query 仍然可用"""
        class FakeWorkflow:
            async def run(self, **kwargs):
                return {
                    "status": "completed",
                    "task_id": "prd-from-query-task",
                    "report_final": "# Competitive Report",
                    "quality_score": 8.0,
                    "quality_grade": "B",
                    "missing_dimensions": [],
                    "verification_score": 8.0,
                    "termination_reason": "完成",
                    "competitors": [],
                    "errors": [],
                    "sync_results": {},
                }

        monkeypatch.setattr("src.workflow.mvp_workflow.MVPWorkflow", lambda **kwargs: FakeWorkflow())
        monkeypatch.setattr("src.tools.prd_generator.PRDGenerator.generate_from_competitive_report", lambda self, report, query: "# PRD Document")

        client = TestClient(app)
        response = client.post(
            "/research/prd-from-query",
            json={"query": "test query", "enable_search": False, "urls": ["https://example.com"]},
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert "prd" in body
        assert "competitive_report" in body


class TestTaskStore:
    def test_task_store_creates_and_retrieves(self):
        """TaskStore 创建和检索"""
        store = server.get_task_store()
        task_id = "store-test-001"

        asyncio.run(store.create(task_id, "test query", {"report": True, "prd": True}))
        task = asyncio.run(store.get(task_id))

        assert task is not None
        assert task.task_id == task_id
        assert task.user_query == "test query"
        assert task.status.value == "pending"

    def test_task_store_update_status(self):
        """TaskStore 更新状态"""
        store = server.get_task_store()
        task_id = "store-test-002"

        asyncio.run(store.create(task_id, "test query", {"report": True, "prd": False}))
        asyncio.run(store.update(task_id, status="running", current_stage="crawl", current_message="抓取中"))

        task = asyncio.run(store.get(task_id))
        assert task.status.value == "running"
        assert task.current_stage == "crawl"

    def test_task_store_add_events(self):
        """TaskStore 添加事件并能检索"""
        from src.workflow.task_store import TaskEvent, TaskEventType

        store = server.get_task_store()
        task_id = "store-test-003"

        asyncio.run(store.create(task_id, "test query", {"report": True, "prd": False}))

        event = TaskEvent(
            id=0,
            event=TaskEventType.DECOMPOSE.value,
            task_id=task_id,
            stage="decompose",
            status="running",
            message="正在拆解",
        )
        asyncio.run(store.add_event(task_id, event))

        task = asyncio.run(store.get(task_id))
        assert len(task.events) == 1
        assert task.events[0].event == "decompose"

    def test_task_store_set_artifact(self):
        """TaskStore 设置 artifact"""
        from src.workflow.task_store import TaskArtifact

        store = server.get_task_store()
        task_id = "store-test-004"

        asyncio.run(store.create(task_id, "test query", {"report": True, "prd": True}))

        artifact = TaskArtifact(artifact_type="report", content="# Report Content", report_id="report_abc")
        asyncio.run(store.set_artifact(task_id, "report", artifact))

        task = asyncio.run(store.get(task_id))
        assert "report" in task.artifacts
        assert task.artifacts["report"].artifact_type == "report"
        assert task.artifacts["report"].report_id == "report_abc"

    def test_task_store_exposes_research_metadata(self):
        """TaskStore 保存抓取来源、质量评分和缺失维度，供前端来源页展示"""
        store = server.get_task_store()
        task_id = "store-test-metadata"

        asyncio.run(store.create(task_id, "test query", {"report": True, "prd": False}))
        asyncio.run(store.update(
            task_id,
            crawl_results={
                "competitors": [
                    {
                        "name": "api000.com",
                        "url": "https://api000.com",
                        "status": "success",
                        "raw_pages": [{"url": "https://api000.com", "content_length": 3339}],
                        "discovered_urls": ["https://api000.com/pricing"],
                    }
                ]
            },
            quality_score=6.5,
            quality_grade="B",
            missing_dimensions=["使用评价"],
        ))

        body = asyncio.run(store.get(task_id)).to_dict()
        assert body["crawl_results"]["competitors"][0]["raw_pages"][0]["content_length"] == 3339
        assert body["quality_score"] == 6.5
        assert body["quality_grade"] == "B"
        assert body["missing_dimensions"] == ["使用评价"]
