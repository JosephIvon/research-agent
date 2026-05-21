"""
E2E tests for the research agent - tests the full stack from API to workflow completion.

These tests require the backend server to be running. Set E2E_API_URL and E2E_FRONTEND_URL
environment variables if using non-default addresses.
"""
import pytest
import time
import requests
import uuid
import os


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


def test_health_check(api_base_url):
    """Verify the /health endpoint returns 200."""
    response = requests.get(f"{api_base_url}/health", timeout=10)
    assert response.status_code == 200


def test_create_research_task(api_base_url):
    """POST to /research/tasks with minimal payload, verify task_id is returned."""
    headers = _auth_headers(api_base_url)
    payload = {
        "query": "test competitor analysis",
        "urls": [],
        "enable_search": False,
    }
    response = requests.post(f"{api_base_url}/research/tasks", json=payload, headers=headers, timeout=10)
    assert response.status_code == 200
    data = response.json()
    assert "task_id" in data
    assert data["task_id"] is not None


def test_sse_connection(api_base_url):
    """Verify SSE endpoint returns an event stream with at least a task_created event."""
    headers = _auth_headers(api_base_url)
    # First create a task
    payload = {
        "query": "test competitor analysis",
        "urls": [],
        "enable_search": False,
    }
    create_response = requests.post(f"{api_base_url}/research/tasks", json=payload, headers=headers, timeout=10)
    assert create_response.status_code == 200
    task_id = create_response.json()["task_id"]

    # Connect to SSE stream
    sse_url = f"{api_base_url}/research/tasks/{task_id}/events"
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


def test_full_research_flow(api_base_url):
    """
    Create a research task and wait for completion.
    Uses a simple query with auto-search only (no external URL dependencies).
    """
    if not os.environ.get("E2E_RUN_FULL_FLOW"):
        pytest.skip("Full research flow requires external search/LLM credentials; set E2E_RUN_FULL_FLOW=1 to run it.")

    headers = _auth_headers(api_base_url)
    # Create task
    payload = {
        "query": "test competitor analysis",
        "urls": [],
        "enable_search": True,
    }
    create_response = requests.post(f"{api_base_url}/research/tasks", json=payload, headers=headers, timeout=30)
    assert create_response.status_code == 200
    task_id = create_response.json()["task_id"]

    # Poll SSE until completion
    sse_url = f"{api_base_url}/research/tasks/{task_id}/events"
    max_wait = 300  # 5 minutes
    start = time.time()
    final_data = None
    completed = False

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
                        if event_type == "completed":
                            final_data = data_str
                            completed = True
                            break
                        elif event_type == "error":
                            pytest.fail(f"Task failed during E2E test: {data_str}")
                if completed:
                    break

    assert completed, f"Task did not complete within {max_wait} seconds"
    assert final_data is not None
