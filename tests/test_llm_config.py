"""LLM provider 配置体检的测试。

要证明的性质：
1. 选中的 provider 密钥不可用时能被**判定**出来（占位符/缺失/过短/未登记）
2. 未选中的 provider 没配密钥**不算错**（多 provider 部署本来就只用一个）
3. 判定是纯配置层的，**不发任何网络请求**（它会被 /health 调用）
4. 返回值**不含密钥本身**
5. 生产环境启动即失败，非生产只警告
6. `_PROVIDERS` 里新增的 provider 必须同步登记，否则测试变红
"""
import inspect
import os
import subprocess
import sys
import textwrap

import pytest

from src.config import settings as st

SELECTED_ONLY_NOTE = "只检查被选中的 provider"


@pytest.fixture
def credential(monkeypatch):
    """设置/清除某个 provider 密钥的**模块常量**（必要时连带环境变量）。

    只 `monkeypatch.setenv` 是不够的、也是原来的 bug：`_llm_credential_value()`
    按设计优先读 `globals()`——因为 `src/llm` 的客户端用的就是 `settings.MINIMAX_API_KEY`
    这类模块常量。模块常量在 import 时已被 `load_dotenv()` 从本机 `.env` 填好，
    于是 setenv 完全不生效：

    - 本机有 `.env` 时，"缺密钥应判 missing" 这类用例会红；
    - 更糟的是反向用例**碰巧**绿——`test_healthy_key_passes` 设的 "a"*40 根本没生效，
      它绿是因为 `.env` 里本来就有一把真 key。测的是运气不是行为。

    CI 没有 `.env` 所以一直没暴露。有 `.env` 的开发者跑测试才是常态。
    """

    def _set(env_key, value):
        monkeypatch.setattr(st, env_key, value, raising=False)
        if value is None:
            monkeypatch.delenv(env_key, raising=False)
        else:
            monkeypatch.setenv(env_key, value)

    return _set


# --------------------------------------------------------------------------
# 判定逻辑
# --------------------------------------------------------------------------

def test_missing_key_is_detected(credential):
    credential("MINIMAX_API_KEY", None)
    result = st.assess_llm_credential("minimax")

    assert result["credential"] == st.LLM_CREDENTIAL_MISSING
    assert st.llm_credential_usable("minimax") is False
    assert "MINIMAX_API_KEY" in result["message"]


def test_placeholder_key_is_detected(credential):
    """这正是 .env.example 的默认值——此前要等真实请求才 401。"""
    credential("MINIMAX_API_KEY", "YOUR_KEY_HERE")
    result = st.assess_llm_credential("minimax")

    assert result["credential"] == st.LLM_CREDENTIAL_PLACEHOLDER
    assert st.llm_credential_usable("minimax") is False


def test_change_me_placeholder_is_detected(credential):
    credential("DEEPSEEK_API_KEY", "CHANGE_ME")
    assert st.assess_llm_credential("deepseek")["credential"] == st.LLM_CREDENTIAL_PLACEHOLDER


def test_too_short_key_is_detected(credential):
    credential("MINIMAX_API_KEY", "sk-short-but-real-looking")
    result = st.assess_llm_credential("minimax")

    assert result["credential"] == st.LLM_CREDENTIAL_TOO_SHORT
    assert "32" in result["message"]


def test_healthy_key_passes(credential):
    credential("MINIMAX_API_KEY", "a" * 40)
    result = st.assess_llm_credential("minimax")

    assert result["credential"] == st.LLM_CREDENTIAL_OK
    assert st.llm_credential_usable("minimax") is True
    assert result["message"] == ""


def test_unselected_providers_are_not_flagged(credential):
    """只查选中的那个：其余没配是正常的，不该制造噪声。"""
    credential("MINIMAX_API_KEY", "a" * 40)
    credential("OPENAI_API_KEY", None)
    credential("DEEPSEEK_API_KEY", None)

    assert st.llm_credential_usable("minimax") is True, SELECTED_ONLY_NOTE


def test_unknown_provider_is_reported_not_silently_ok():
    result = st.assess_llm_credential("totally-unknown")
    assert result["credential"] == st.LLM_CREDENTIAL_UNKNOWN_PROVIDER
    assert st.llm_credential_usable("totally-unknown") is False


@pytest.mark.parametrize("provider", sorted(st.LLM_PROVIDERS_WITHOUT_KEY))
def test_keyless_providers_need_no_credential(provider):
    """离线脚本化 provider（M1 的 ScriptedLLMClient）必须免密钥。"""
    assert st.llm_credential_usable(provider) is True


# --------------------------------------------------------------------------
# 不泄露密钥
# --------------------------------------------------------------------------

def test_assessment_never_echoes_the_key(credential):
    secret = "sk-super-secret-value-that-must-not-leak"
    credential("MINIMAX_API_KEY", secret)
    credential("OPENAI_API_KEY", secret[:5])

    blob = repr(st.assess_llm_credential("minimax")) + repr(st.assess_llm_credential("openai"))
    assert secret not in blob
    assert secret[:5] not in blob


def test_message_mentions_the_env_var_name_not_the_value(credential):
    credential("MINIMAX_API_KEY", "YOUR_KEY_HERE")
    result = st.assess_llm_credential("minimax")
    assert "MINIMAX_API_KEY" in result["message"]
    assert "YOUR_KEY_HERE" not in result["message"]


# --------------------------------------------------------------------------
# 纯配置层：不发网络请求
# --------------------------------------------------------------------------

def test_assessment_performs_no_network_io(credential, monkeypatch):
    """它会被 /health 调用，健康检查不该有副作用或烧配额。

    用**记录调用**而不是抛异常：变异若把网络调用包在 `try/except` 里，
    抛出的 AssertionError 会被吞掉，测试照样绿——记录法抓不到这种掩盖。
    """
    import socket

    attempts = []

    def record(*args, **kwargs):
        attempts.append((args, kwargs))
        raise OSError("network disabled for this test")

    monkeypatch.setattr(socket.socket, "connect", record)
    monkeypatch.setattr(socket, "create_connection", record)
    credential("MINIMAX_API_KEY", "a" * 40)

    assert st.assess_llm_credential("minimax")["credential"] == st.LLM_CREDENTIAL_OK
    assert attempts == [], f"assess_llm_credential 发起了网络连接：{attempts}"


def test_assessment_is_deterministic(credential):
    credential("MINIMAX_API_KEY", "YOUR_KEY_HERE")
    first = st.assess_llm_credential("minimax")
    assert all(st.assess_llm_credential("minimax") == first for _ in range(5))


def test_credential_is_read_from_the_module_constant_not_the_env(monkeypatch):
    """体检读的值必须和 `src/llm` 客户端实际拿到的值是**同一份**。

    客户端用的是 `settings.MINIMAX_API_KEY` 模块常量，不是每次 os.getenv。
    若体检改成只读 os.getenv，两者一分叉，健康检查就会对真实使用的密钥说谎——
    而且这种情况在本地几乎看不出来（两者通常同值）。

    刻意只改模块常量、不碰环境变量，让两者分叉，断言判定跟着常量走。
    """
    monkeypatch.setattr(st, "MINIMAX_API_KEY", "YOUR_KEY_HERE", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "a" * 40)

    assert st.assess_llm_credential("minimax")["credential"] == st.LLM_CREDENTIAL_PLACEHOLDER


# --------------------------------------------------------------------------
# 与 llm 层的注册表保持一致
# --------------------------------------------------------------------------

def test_every_registered_provider_is_classified():
    """M1 新增 provider 时必须同步登记到两张表之一。

    漏登记的后果是：真实 provider 被当成"未登记"，要么误判不可用、
    要么（更糟）漏密钥检查静默通过——这正是要防的。
    """
    from src.llm import _PROVIDERS

    known = set(st.LLM_PROVIDER_KEYS) | st.LLM_PROVIDERS_WITHOUT_KEY
    unregistered = set(_PROVIDERS) - known
    assert not unregistered, (
        f"新增 provider 未在 settings 登记：{sorted(unregistered)}。"
        "请加入 LLM_PROVIDER_KEYS（需密钥）或 LLM_PROVIDERS_WITHOUT_KEY（免密钥）。"
    )


def test_classified_providers_do_not_drift_into_the_other_table():
    assert not (set(st.LLM_PROVIDER_KEYS) & st.LLM_PROVIDERS_WITHOUT_KEY), \
        "同一 provider 不能既要求密钥又免密钥"


# --------------------------------------------------------------------------
# 生产 fail-closed（子进程隔离，避免污染当前测试进程）
# --------------------------------------------------------------------------

IMPORT_SNIPPET = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {repo!r})
    try:
        import src.config.settings
    except ValueError as exc:
        print("RAISED:", exc)
    else:
        print("IMPORTED")
    """
)


def _import_settings_with(env_overrides):
    """在全新解释器里 import settings。

    PYTHON_DOTENV_DISABLED=1 屏蔽开发者本机的 .env——否则"缺密钥应当拒绝启动"
    的断言会被本机真 key 顶成假绿（CI 无 .env 所以那边一直是绿的）。
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = IMPORT_SNIPPET.format(repo=repo)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLM_", "MINIMAX_", "OPENAI_", "DEEPSEEK_", "GLM_", "DOUBAO_"))}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    return proc.stdout.strip()


def test_production_refuses_to_start_with_placeholder_key():
    out = _import_settings_with({
        "APP_ENV": "production",
        "LLM_PROVIDER": "minimax",
        "MINIMAX_API_KEY": "YOUR_KEY_HERE",
        "JWT_SECRET_KEY": "b" * 40,
    })
    assert out.startswith("RAISED:"), out
    assert "minimax" in out


def test_production_refuses_to_start_with_missing_key():
    out = _import_settings_with({
        "APP_ENV": "production",
        "LLM_PROVIDER": "minimax",
        "JWT_SECRET_KEY": "b" * 40,
    })
    assert out.startswith("RAISED:"), out


def test_production_starts_with_a_real_key():
    out = _import_settings_with({
        "APP_ENV": "production",
        "LLM_PROVIDER": "minimax",
        "MINIMAX_API_KEY": "c" * 40,
        "JWT_SECRET_KEY": "b" * 40,
        "ALLOWED_HOSTS": "example.test",
    })
    assert out == "IMPORTED", out


def test_production_allows_keyless_provider():
    """M1 的 fake provider 在生产配置下也必须能起来。"""
    out = _import_settings_with({
        "APP_ENV": "production",
        "LLM_PROVIDER": "fake",
        "JWT_SECRET_KEY": "b" * 40,
        "ALLOWED_HOSTS": "example.test",
    })
    assert out == "IMPORTED", out


def test_development_only_warns_and_still_imports():
    """开发与测试经常没有真实密钥，硬失败会挡住离线流程。"""
    out = _import_settings_with({
        "APP_ENV": "development",
        "LLM_PROVIDER": "minimax",
    })
    assert out == "IMPORTED", out


# --------------------------------------------------------------------------
# /health 与 /health/ready 的分工
# --------------------------------------------------------------------------

def test_health_stays_healthy_even_when_llm_key_is_missing(credential):
    """CI 与 docker healthcheck 用的就是 /health，而它们本来就没有真实密钥。

    若这里因为密钥缺失改判 unhealthy，探针会天天假红——那是把可用性检查
    变成配置检查，反而让人忽略真正的挂掉。

    真调用而不是 grep 源码：把 `"status": "healthy"` 改成三元表达式后，
    源码里仍然能搜到那个字符串，只有实际调用才能分辨。
    """
    import server

    credential("MINIMAX_API_KEY", "YOUR_KEY_HERE")
    result = server.health()

    assert result["status"] == "healthy", "缺密钥不得让存活探针变红"
    assert result["llm"]["credential_usable"] is False, "但必须如实报告不可用"


def test_ready_endpoint_reports_not_ready_payload(credential):
    """不可用时返回的是带 503 的 JSONResponse，body 里必须带可执行的修复建议。"""
    import json
    import server

    credential("MINIMAX_API_KEY", "YOUR_KEY_HERE")
    response = server.health_ready()

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["status"] == "not_ready"
    assert payload["llm"]["credential"] == st.LLM_CREDENTIAL_PLACEHOLDER
    assert payload["llm"]["remedy"], "不可用时必须给出可执行的修复建议"
    assert "YOUR_KEY_HERE" not in response.body.decode()


def test_ready_endpoint_returns_http_503_over_http(credential):
    """端到端断言状态码——只看返回值抓不住"没走 JSONResponse"这种变异。"""
    from fastapi.testclient import TestClient
    import server

    credential("MINIMAX_API_KEY", None)
    with TestClient(server.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_ready_endpoint_returns_200_when_credential_is_usable(credential):
    from fastapi.testclient import TestClient
    import server

    credential("MINIMAX_API_KEY", "a" * 40)
    with TestClient(server.app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_health_never_returns_the_key(credential):
    import server

    credential("MINIMAX_API_KEY", "YOUR_KEY_HERE")
    result = server.health()
    blob = repr(result)
    assert "YOUR_KEY_HERE" not in blob
    assert result["llm"]["credential"] == st.LLM_CREDENTIAL_PLACEHOLDER
    assert result["llm"]["credential_usable"] is False
    assert result["llm"]["remedy"], "不可用时必须给出可执行的修复建议"


def test_ready_endpoint_is_public():
    """编排系统通常不持有凭据，就绪探针必须免鉴权。"""
    import server

    assert "/health/ready" in server.PUBLIC_PATHS