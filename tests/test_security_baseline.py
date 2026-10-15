import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from server import app
from src.llm import OpenAICompatibleClient
from src.security.url_validation import UnsafeURL, validate_fetch_url


def test_tools_package_imports_without_eager_search_instance():
    """导入 src.tools 不得实例化任何懒加载单例。

    这个不变量只能在**全新解释器**里验证：只要本进程里任何测试调用过
    `get_search_tool()`，模块级单例就不再是 None，原来的写法等于在断言
    "测试执行顺序碰巧如此"——任何在它之前构造 CoordinatorAgent 的用例都会
    让它失败。因此把懒加载断言放进子进程做一次干净的 import。
    """
    # 身份一致性检查与执行顺序无关，保留在同进程做
    import src.tools as tools
    import src.tools.followup_optimizer as followup_module
    import src.tools.multi_role_reviewer as reviewer_module
    import src.tools.prd_generator as prd_module
    import src.tools.search_tool as search_module

    assert tools.get_search_tool is search_module.get_search_tool
    assert tools.get_follow_up_optimizer is followup_module.get_follow_up_optimizer
    assert tools.get_multi_role_reviewer is reviewer_module.get_multi_role_reviewer
    assert tools.get_prd_generator is prd_module.get_prd_generator

    probe = """
import src.tools as tools
import src.tools.followup_optimizer as followup_module
import src.tools.multi_role_reviewer as reviewer_module
import src.tools.prd_generator as prd_module
import src.tools.search_tool as search_module

assert search_module._search_tool_instance is None, "search_tool 被急切实例化"
assert followup_module._follow_up_optimizer_instance is None, "followup 优化器被急切实例化"
assert reviewer_module._multi_role_reviewer_instance is None, "多角色审查器被急切实例化"
assert prd_module._prd_generator_instance is None, "PRD 生成器被急切实例化"
assert not hasattr(search_module, "search_tool"), "仍导出模块级 search 实例"
print("CLEAN_IMPORT")
"""
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": root,
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=root,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"子进程中的懒加载断言失败：\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "CLEAN_IMPORT" in result.stdout


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


def test_research_request_rejects_private_url_before_workflow_runs(auth_headers):
    client = TestClient(app)
    response = client.post(
        "/research/competitive",
        json={"query": "test", "urls": ["http://127.0.0.1:8080/health"], "enable_search": False},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_competitive_request_accepts_per_site_credentials(monkeypatch, auth_headers):
    import src.workflow.mvp_workflow as workflow_module

    captured = {}

    class FakeWorkflow:
        async def run(self, **kwargs):
            captured.update(kwargs)
            return {
                "status": "completed",
                "task_id": "task-1",
                "report_final": "# OK",
                "competitors": [],
            }

    monkeypatch.setattr(workflow_module, "MVPWorkflow", FakeWorkflow)
    client = TestClient(app)
    response = client.post(
        "/research/competitive",
        json={
            "query": "test",
            "enable_search": False,
            "target_sites": [
                {
                    "url": "https://93.184.216.34/product-a",
                    "login_url": "https://93.184.216.34/login",
                    "auth_credentials": {"username": "alice", "password": "secret"},
                },
                {"url": "https://93.184.216.34/product-b"},
            ],
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert captured["target_sites"][0]["auth_credentials"] == {"username": "alice", "password": "secret"}
    assert captured["target_sites"][0]["login_url"] == "https://93.184.216.34/login"
    assert captured["target_sites"][1]["auth_credentials"] is None


@pytest.mark.asyncio
async def test_fetch_all_competitors_uses_per_site_credentials(monkeypatch):
    """每站的凭据必须**逐站**传下去，不能串味（alpha 的账号不能发给 beta）。

    注意必须同时替换 `WebCrawler`：`_fetch_all_competitors` 现在整批共享一个
    爬虫实例（`async with WebCrawler() as shared_crawler`），只替换
    `_fetch_single_competitor` 拦不住上下文管理器——它会真的去启动 playwright，
    本机没装浏览器时抛的是 BrowserType.launch 而不是一个能指明问题的断言失败。
    """
    from src.workflow import mvp_workflow as wf

    workflow = object.__new__(wf.MVPWorkflow)
    calls = []

    class _FakeCrawler:
        """整批共享实例的替身：不碰 playwright，也不发起任何网络请求。"""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_fetch(url, auth_credentials=None, login_url=None,
                         custom_selectors=None, crawler=None):
        calls.append((url, auth_credentials, login_url))
        return {"name": url, "url": url, "extracted_data": [], "status": "success", "error_message": None}

    monkeypatch.setattr(wf, "WebCrawler", _FakeCrawler)
    monkeypatch.setattr(workflow, "_fetch_single_competitor", fake_fetch)
    await workflow._fetch_all_competitors([
        {
            "url": "https://alpha.example/product",
            "auth_credentials": {"username": "alpha", "password": "one"},
            "login_url": "https://alpha.example/login",
        },
        {
            "url": "https://beta.example/product",
            "auth_credentials": {"username": "beta", "password": "two"},
            "login_url": "https://beta.example/signin",
        },
    ])

    assert calls == [
        ("https://alpha.example/product", {"username": "alpha", "password": "one"}, "https://alpha.example/login"),
        ("https://beta.example/product", {"username": "beta", "password": "two"}, "https://beta.example/signin"),
    ]


def test_optional_bearer_auth_protects_research_endpoints(monkeypatch):
    monkeypatch.setattr(server, "API_AUTH_TOKEN", "test-token")
    client = TestClient(app)

    protected = client.post("/research/competitive", json={"query": "test", "enable_search": False})
    public = client.get("/health")

    assert protected.status_code == 401
    assert public.status_code == 200


def test_history_endpoint_reads_only_report_files(monkeypatch, tmp_path, admin_headers):
    """文件兜底分支只对 admin 开放。

    这类报告在库里没有归属记录、归属未知，非 admin 看不到是有意为之的设计
    （见 server._can_access_report）；跨用户隔离由 tests/test_tenant_isolation.py 覆盖。
    """
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    report_path = tmp_path / "research_report_20260517_120000.md"
    report_path.write_text("# Smoke Report\n\n上线检查内容", encoding="utf-8")
    (tmp_path / "notes.md").write_text("ignored", encoding="utf-8")

    client = TestClient(app)
    list_response = client.get("/research/history", headers=admin_headers)
    detail_response = client.get("/research/history/research_report_20260517_120000", headers=admin_headers)
    traversal_response = client.get("/research/history/../notes", headers=admin_headers)

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
