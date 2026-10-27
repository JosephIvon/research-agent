"""文档平台凭据：可由管理员在界面写入，保存后同步立即生效。

背景：凭据此前只能来自进程启动时的环境变量——界面上既没有输入框也没有写入
通路，同步页的「连接」按钮把人指向一个**无法完成配置**的页面。同步层还有
第二重固化：`FeishuSync` 构造时读一次 env、又被进程级单例缓存住，运行期改
环境变量不生效。

这组用例钉住：
- 写入 → 读取 → 解析的闭环，且**已存的优先于环境变量**
- 解析在**调用时**发生（不是构造时）
- 对外只回显掩码后的 app_id，secret 不出服务端
"""
import os

import pytest


@pytest.fixture
def cred_file(tmp_path, monkeypatch):
    """凭据写到临时目录——绝不能写进仓库。"""
    path = tmp_path / "doc_sync_credentials.json"
    monkeypatch.setenv("DOC_SYNC_CREDENTIALS_FILE", str(path))
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET",
                "TENCENT_APP_ID", "TENCENT_CLIENT_ID", "TENCENT_APP_SECRET"):
        monkeypatch.delenv(key, raising=False)
    return path


def test_saved_credentials_round_trip(cred_file):
    from src.config.credentials import load_credentials, save_credentials

    save_credentials("feishu", "cli_abc123", "secret-value")
    stored = load_credentials()
    assert stored["feishu"]["app_id"] == "cli_abc123"
    assert stored["feishu"]["app_secret"] == "secret-value"
    assert cred_file.exists(), "凭据必须真的落盘"


def test_resolve_prefers_stored_over_environment(cred_file, monkeypatch):
    """管理员在界面存的凭据应当胜过环境变量——否则「保存成功」是假的。"""
    from src.config.credentials import resolve_credentials, save_credentials

    monkeypatch.setenv("FEISHU_APP_ID", "env_id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "env_secret")
    save_credentials("feishu", "stored_id", "stored_secret")

    assert resolve_credentials("feishu") == ("stored_id", "stored_secret")


def test_resolve_falls_back_to_environment(cred_file, monkeypatch):
    from src.config.credentials import resolve_credentials

    monkeypatch.setenv("FEISHU_APP_ID", "env_id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "env_secret")
    assert resolve_credentials("feishu") == ("env_id", "env_secret")


def test_resolve_without_anything_returns_nones(cred_file):
    from src.config.credentials import resolve_credentials

    assert resolve_credentials("feishu") == (None, None)


def test_tencent_accepts_client_id_alias(cred_file, monkeypatch):
    """腾讯的 app id 环境变量有两个别名，都要认。"""
    from src.config.credentials import resolve_credentials

    monkeypatch.setenv("TENCENT_CLIENT_ID", "tencent_via_alias")
    monkeypatch.setenv("TENCENT_APP_SECRET", "secret")
    assert resolve_credentials("tencent") == ("tencent_via_alias", "secret")


def test_status_never_leaks_the_secret(cred_file):
    """对外状态里不能出现 secret——这个函数喂的是公开端点。"""
    from src.config.credentials import credential_status, save_credentials

    save_credentials("feishu", "cli_abc123", "super-secret-value")
    status = credential_status("feishu")
    assert status["configured"] is True
    assert status["source"] == "stored"
    assert "super-secret-value" not in str(status)
    assert status["app_id_masked"] != "cli_abc123", "app_id 也要掩码，不能原样回显"


def test_broken_credential_file_does_not_block_sync(cred_file):
    """坏文件不能让整个同步功能瘫掉——退回环境变量即可。"""
    from src.config.credentials import load_credentials, resolve_credentials

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("{ 这不是 json", encoding="utf-8")
    assert load_credentials() == {}
    assert resolve_credentials("feishu") == (None, None)


def test_default_location_is_outside_the_repository_tracked_files(cred_file, monkeypatch):
    """默认落点必须在 .local/ 下——那个目录已被 .gitignore 排除。"""
    monkeypatch.delenv("DOC_SYNC_CREDENTIALS_FILE", raising=False)
    from src.config.credentials import credentials_path

    path = credentials_path()
    assert path.parent.name == ".local", f"默认落点应在 .local/ 下，实际 {path}"
    assert path.name.endswith(".json")


def test_settings_payload_reflects_stored_credentials(cred_file):
    """设置页的「已配置/未配置」必须与真实可用的凭据一致。"""
    from server import _safe_settings_payload
    from src.config.credentials import save_credentials

    payload = _safe_settings_payload()
    assert payload["feishu_configured"] is False

    save_credentials("feishu", "cli_abc123", "secret-value")
    payload = _safe_settings_payload()
    assert payload["feishu_configured"] is True, "存了凭据却仍显示未配置"
    assert "secret-value" not in str(payload)


def test_sync_reads_credentials_at_call_time(cred_file, monkeypatch):
    """核心修复：凭据在**调用时**解析，保存后不必重启进程。"""
    import src.sync as sync_mod
    from src.config.credentials import save_credentials

    captured = {}

    class _CaptureSync:
        def __init__(self, app_id=None, app_secret=None):
            captured["app_id"] = app_id
            captured["app_secret"] = app_secret

        def sync_markdown(self, content, title=None):
            return {"platform": "feishu", "ok": True}

    monkeypatch.setattr(sync_mod, "FeishuSync", _CaptureSync)
    save_credentials("feishu", "cli_abc123", "secret-value")

    manager = sync_mod.DocSyncManager()
    result = manager.sync_to_feishu("# 正文", "标题")

    assert result.get("ok") is True
    assert captured.get("app_id") == "cli_abc123", "同步时没有用上刚存的凭据"
    assert captured.get("app_secret") == "secret-value"


def test_saved_credentials_file_is_gitignored():
    """凭据文件绝不能被 git 收进去。"""
    import subprocess

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".local/doc_sync_credentials.json"],
        cwd=repo, capture_output=True,
    )
    assert result.returncode == 0, ".local/ 必须被 .gitignore 排除，否则密钥会进仓库"
