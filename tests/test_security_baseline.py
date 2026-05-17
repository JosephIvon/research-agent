import pytest
from fastapi.testclient import TestClient

import server
from server import app
from src.llm import OpenAICompatibleClient
from src.security.url_validation import UnsafeURL, validate_fetch_url


def test_validate_fetch_url_rejects_private_and_non_http_urls():
    blocked_urls = [
        "http://127.0.0.1:8080/health",
        "http://localhost/admin",
        "http://10.0.0.1/",
        "file:///etc/passwd",
        "https://user:secret@example.com/",
    ]

    for url in blocked_urls:
        with pytest.raises(UnsafeURL):
            validate_fetch_url(url)


def test_validate_fetch_url_allows_public_https_url():
    assert validate_fetch_url("https://93.184.216.34/path") == "https://93.184.216.34/path"


def test_base_url_normalization_does_not_double_version_suffix():
    assert OpenAICompatibleClient._normalize_base_url("https://api.minimax.chat") == "https://api.minimax.chat/v1"
    assert OpenAICompatibleClient._normalize_base_url("https://api.minimax.chat/v1") == "https://api.minimax.chat/v1"
    assert OpenAICompatibleClient._normalize_base_url("https://api.minimax.chat/v1/") == "https://api.minimax.chat/v1"


def test_base_url_normalization_preserves_non_v1_suffix():
    assert OpenAICompatibleClient._normalize_base_url("https://open.bigmodel.cn/api/paas/v4") == "https://open.bigmodel.cn/api/paas/v4"
    assert OpenAICompatibleClient._normalize_base_url("https://open.bigmodel.cn/api/paas/v4/") == "https://open.bigmodel.cn/api/paas/v4"
    assert OpenAICompatibleClient._normalize_base_url("https://api.example.com/v2") == "https://api.example.com/v2"


def test_research_request_rejects_private_url_before_workflow_runs():
    client = TestClient(app)
    response = client.post(
        "/research/competitive",
        json={"query": "test", "urls": ["http://127.0.0.1:8080/health"], "enable_search": False},
    )

    assert response.status_code == 422


def test_optional_bearer_auth_protects_research_endpoints(monkeypatch):
    monkeypatch.setattr(server, "API_AUTH_TOKEN", "test-token")
    client = TestClient(app)

    protected = client.post("/research/competitive", json={"query": "test", "enable_search": False})
    public = client.get("/health")

    assert protected.status_code == 401
    assert public.status_code == 200


def test_history_endpoint_reads_only_report_files(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    report_path = tmp_path / "research_report_20260517_120000.md"
    report_path.write_text("# Smoke Report\n\n上线检查内容", encoding="utf-8")
    (tmp_path / "notes.md").write_text("ignored", encoding="utf-8")

    client = TestClient(app)
    list_response = client.get("/research/history")
    detail_response = client.get("/research/history/research_report_20260517_120000")
    traversal_response = client.get("/research/history/../notes")

    assert list_response.status_code == 200
    assert list_response.json()[0]["id"] == "research_report_20260517_120000"
    assert detail_response.status_code == 200
    assert detail_response.json()["markdown"].startswith("# Smoke Report")
    assert traversal_response.status_code == 404


def test_settings_endpoint_exposes_status_not_secret_values(monkeypatch):
    monkeypatch.setattr(server, "MINIMAX_API_KEY", "secret-minimax-key")
    monkeypatch.setattr(server, "API_AUTH_TOKEN", "secret-api-token")

    client = TestClient(app)
    response = client.get("/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["minimax_configured"] is True
    assert body["api_auth_enabled"] is True
    assert "secret-minimax-key" not in response.text
    assert "secret-api-token" not in response.text
