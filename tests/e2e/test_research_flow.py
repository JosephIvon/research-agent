"""
E2E tests for the research agent - tests the full stack from API to workflow completion.

These tests require the backend server to be running. Set E2E_API_URL and E2E_FRONTEND_URL
environment variables if using non-default addresses.

Two test intents, kept apart on purpose (see tests/e2e/conftest.py):

* ``contract`` - health / task creation / SSE contract. Needs only a running
  backend, no credentials. CI runs these on every push and pull request.
* ``real_services`` - the full search + LLM pipeline. Needs provider secrets, so
  CI runs it in a separate, credential-gated job.

An unreachable backend or missing credentials produces a *tagged* skip
(``E2E-NO-BACKEND`` / ``E2E-NO-CREDENTIALS``), never a quiet pass.
"""
import pytest
import time
import requests
import uuid


def _auth_headers(api_base_url):
    username = f"e2e_{uuid.uuid4().hex[:12]}"
    password = f"pw_{uuid.uuid4().hex}"
    response = requests.post(
        f"{api_base_url}/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": password},
        timeout=10,
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.contract
def test_health_check(live_backend):
    """Verify the /health endpoint returns 200."""
    response = requests.get(f"{live_backend}/health", timeout=10)
    assert response.status_code == 200


@pytest.mark.contract
def test_create_research_task(live_backend):
    """POST to /research/tasks with minimal payload, verify task_id is returned."""
    headers = _auth_headers(live_backend)
    payload = {
        "query": "test competitor analysis",
        "urls": [],
        "enable_search": False,
    }
    response = requests.post(f"{live_backend}/research/tasks", json=payload, headers=headers, timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    assert data["task_id"] is not None


@pytest.mark.contract
def test_sse_connection(live_backend):
    """Verify SSE endpoint returns an event stream with at least a task_created event."""
    headers = _auth_headers(live_backend)
    # First create a task
    payload = {
        "query": "test competitor analysis",
        "urls": [],
        "enable_search": False,
    }
    create_response = requests.post(f"{live_backend}/research/tasks", json=payload, headers=headers, timeout=10)
    assert create_response.status_code == 200
    task_id = create_response.json()["task_id"]

    # Connect to SSE stream
    sse_url = f"{live_backend}/research/tasks/{task_id}/events"
    events_received = {}

    sse_headers = {**headers, "Accept": "text/event-stream"}
    with requests.get(sse_url, headers=sse_headers, stream=True, timeout=30) as resp:
        assert resp.status_code == 200
        buffer = ""
        for chunk in resp.iter_content(chunk_size=256):
            if chunk:
                buffer += chunk.decode("utf-8")
                # SSE events are separated by blank lines (\n\n)
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    event_type = None
                    data_lines = []
                    for line in block.split("\n"):
                        if line.startswith("event: "):
                            event_type = line[7:].strip()
                        elif line.startswith("data: "):
                            data_lines.append(line[6:].strip())
                    if data_lines and event_type:
                        events_received[event_type] = "\n".join(data_lines)
                        if event_type == "task_created":
                            break
                if "task_created" in events_received:
                    break

    assert "task_created" in events_received, f"Expected task_created event, got: {events_received}"


@pytest.mark.real_services
def test_full_research_flow(real_service_credentials, live_backend):
    """
    Create a research task and wait for completion.

    用**固定稳定目标 URL**而不是自动搜索：真实搜索命中什么完全不可控
    （营销页/反爬页/大页面超时），把任意公网站点抓取失败当成 E2E 失败
    会让真服务测试 flaky——抓取失败是产品层有降级设计的正常现象。
    目标是极稳定的官网首页，验证"LLM 真调用 → 抓取 → 抽取 → 引用校验
    → 报告产出"整条真链路能跑通。

    Credential check comes first so a credential-less run reports the credential
    reason instead of a misleading backend reason.
    """
    headers = _auth_headers(live_backend)
    # 稳定目标：维基百科页——内容厚（门禁易过）、无登录墙、无广告反爬，
    # 2026 实测可从 CI 容器稳定抓取。用固定 URL 而非自动搜索（搜索命中什么
    # 不可控，营销页/反爬页会让真服务测试 flaky）。
    payload = {
        "query": "调研竞品分析方法论",
        "urls": ["https://en.wikipedia.org/wiki/Competitor_analysis"],
        "enable_search": False,
    }
    create_response = requests.post(f"{live_backend}/research/tasks", json=payload, headers=headers, timeout=30)
    assert create_response.status_code == 200
    task_id = create_response.json()["task_id"]

    # Poll SSE until completion
    sse_url = f"{live_backend}/research/tasks/{task_id}/events"
    max_wait = 300  # 5 minutes
    start = time.time()
    final_data = None
    completed = False
    terminal_error = None

    sse_headers = {**headers, "Accept": "text/event-stream"}
    with requests.get(sse_url, headers=sse_headers, stream=True, timeout=max_wait) as resp:
        assert resp.status_code == 200
        buffer = ""
        for chunk in resp.iter_content(chunk_size=256):
            elapsed = time.time() - start
            if elapsed > max_wait:
                break
            if chunk:
                buffer += chunk.decode("utf-8")
                # SSE events are separated by blank lines (\n\n)
                while "\n\n" in buffer:
                    block, buffer = buffer.split("\n\n", 1)
                    event_type = None
                    data_lines = []
                    for line in block.split("\n"):
                        if line.startswith("event: "):
                            event_type = line[7:].strip()
                        elif line.startswith("data: "):
                            data_lines.append(line[6:].strip())
                    if data_lines and event_type:
                        data_str = "\n".join(data_lines)
                        # 任务级终止事件才算结果；per-site 抓取失败（error/crawl）
                        # 是降级设计，不算任务失败
                        if event_type == "completed":
                            final_data = data_str
                            completed = True
                            break
                        elif event_type == "error" and "crawl" not in event_type:
                            terminal_error = data_str
                if completed:
                    break

    if not completed and terminal_error is None and time.time() - start >= max_wait:
        pytest.fail(f"Task did not complete within {max_wait} seconds")
    assert completed, f"Task did not complete within {max_wait} seconds; last error: {terminal_error}"
    assert final_data is not None
