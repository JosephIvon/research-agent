"""占位符/弱密钥的 fail-closed 回归测试。

覆盖三层防线：
1. src/config/settings.py —— production 环境下 JWT 密钥是占位符/弱值时拒绝加载；
   "完全没设"时的既有报错行为必须原样保留。
2. src/auth/user_db.py —— 占位符/过短的初始口令不建管理员账号，
   更不允许进入 bcrypt。
3. deploy.sh —— 表驱动校验必须真的覆盖到各必检变量（旧实现 grep 的那个
   串在仓库里根本不存在，等于没校验）。

settings 模块在 import 时就会 load_dotenv() 并 os.makedirs(OUTPUT_DIR)，
"加载期是否抛错"受本进程已有环境与真实 .env 污染，只能在全新子进程里验证。

子进程一律带 PYTHON_DOTENV_DISABLED=1 把开发者本机的 .env 整个屏蔽掉。原先只靠
"load_dotenv 默认不覆盖已存在的环境变量"——那只对**显式设了变量**的方向成立。
一旦测的是"完全没设应当报错"（test_production_missing_jwt_secret_keeps_legacy_error），
本机 .env 会把值补回来，断言直接翻红。同一个文件里的正向用例因此长期是
CI 绿、本机红。user_db 相反，monkeypatch 隔离 DB 路径与模块级单例就够了。
"""
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 与 .env.example / deploy.sh 共同构成 P0 的那批占位值
PLACEHOLDER_SECRETS = [
    "CHANGE_ME_GENERATE_A_RANDOM_32_PLUS_CHAR_SECRET",
    "YOUR_32_CHAR_HEX_SECRET",
    "replace_with_32_random_hex_chars",
    "your-secret-goes-here-xxxxxxxxxxxxxxxx",
]


def _import_settings(env_overrides):
    """在全新解释器里 import settings，返回 CompletedProcess。

    PYTHON_DOTENV_DISABLED=1 让 load_dotenv() 变空操作：本机 .env 里若恰好有
    JWT_SECRET_KEY，"未设置应当报错"的断言会被它顶成假绿。
    """
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, "-c", "import src.config.settings"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )


@pytest.mark.parametrize("placeholder", PLACEHOLDER_SECRETS)
def test_production_rejects_placeholder_jwt_secret(placeholder):
    """生产环境用占位符当 JWT 密钥必须拒绝启动——不是"没设才报错"。"""
    result = _import_settings({"APP_ENV": "production", "JWT_SECRET_KEY": placeholder})

    assert result.returncode != 0, (
        f"占位符 JWT_SECRET_KEY 在 production 下被放行：{placeholder}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "JWT_SECRET_KEY" in result.stderr


def test_production_rejects_short_jwt_secret():
    """长度不足 32 位的密钥同样不可用于生产。"""
    result = _import_settings({"APP_ENV": "production", "JWT_SECRET_KEY": "too-short"})

    assert result.returncode != 0, f"弱 JWT_SECRET_KEY 被放行\nstderr={result.stderr}"
    assert "JWT_SECRET_KEY" in result.stderr


def test_production_missing_jwt_secret_keeps_legacy_error():
    """完全没设时的既有报错文案必须保留，不能被新分支悄悄改掉。"""
    env = {key: value for key, value in os.environ.items() if key != "JWT_SECRET_KEY"}
    result = subprocess.run(
        [sys.executable, "-c", "import src.config.settings"],
        capture_output=True,
        text=True,
        # 不屏蔽 .env 的话，本机开发用的 JWT_SECRET_KEY 会被 load_dotenv 补回来，
        # 于是"没设应当报错"的断言翻红——CI 无 .env 所以那边一直是绿的。
        env={**env, "PYTHONPATH": str(PROJECT_ROOT), "APP_ENV": "production",
             "PYTHON_DOTENV_DISABLED": "1"},
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )

    assert result.returncode != 0
    assert "JWT_SECRET_KEY must be set in production" in result.stderr


def test_non_production_allows_placeholder_jwt_secret():
    """开发/测试环境不该被这条规则打断。"""
    result = _import_settings(
        {"APP_ENV": "development", "JWT_SECRET_KEY": "CHANGE_ME_GENERATE_A_RANDOM_32_PLUS_CHAR_SECRET"}
    )

    assert result.returncode == 0, f"非生产环境被误伤\nstdout={result.stdout}\nstderr={result.stderr}"


@pytest.mark.parametrize("password", ["CHANGE_ME", "YOUR_PASSWORD_HERE", "short", "x" * 5])
def test_ensure_initial_admin_refuses_weak_password(monkeypatch, tmp_path, caplog, password):
    """占位符/过短口令既不建号，也不许进 bcrypt。"""
    from src.auth import user_db

    db_path = tmp_path / "users.db"
    monkeypatch.setenv("USER_DB_PATH", str(db_path))
    monkeypatch.setattr(user_db, "INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(user_db, "INITIAL_ADMIN_PASSWORD", password)
    monkeypatch.setattr(user_db, "_user_db", None)

    hashed_inputs = []
    monkeypatch.setattr(
        user_db, "get_password_hash", lambda value: hashed_inputs.append(value) or "fake-hash"
    )

    with caplog.at_level(logging.WARNING):
        user_db.init_user_db()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT username FROM users").fetchall()
    assert rows == [], "占位符口令竟然建出了账号"
    assert hashed_inputs == [], "占位符口令进入了 bcrypt"
    assert any("INITIAL_ADMIN_PASSWORD" in record.getMessage() for record in caplog.records), (
        f"缺少告警日志，实际记录：{[r.getMessage() for r in caplog.records]}"
    )


def test_ensure_initial_admin_still_creates_admin_with_strong_password(monkeypatch, tmp_path):
    """反向对照：合规口令必须照常建号，防止把校验写成"一律不建"。"""
    from src.auth import user_db

    db_path = tmp_path / "users.db"
    monkeypatch.setenv("USER_DB_PATH", str(db_path))
    monkeypatch.setattr(user_db, "INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(user_db, "INITIAL_ADMIN_PASSWORD", "a-strong-password-1")
    monkeypatch.setattr(user_db, "_user_db", None)

    user_db.init_user_db()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT username, role FROM users").fetchall()
    assert rows == [("admin", "admin")]


def test_env_example_placeholders_are_all_detected():
    """示例文件里的安全关键值必须仍能被检测器识别为占位符。

    这条断言锁住的是"检测器与 .env.example 不脱节"：一旦有人把示例值改成
    看起来像真密钥的字符串，或者检测正则漏掉某类占位符，这里立刻红。
    """
    from src.config.settings import is_weak_secret

    example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    watched = {"JWT_SECRET_KEY", "INITIAL_ADMIN_PASSWORD", "SEARXNG_SECRET"}
    checked = 0
    for raw_line in example.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split("#")[0].strip()
        if key not in watched and not key.endswith("_API_KEY"):
            continue
        if not value:
            continue
        assert is_weak_secret(value), f".env.example 中的 {key} 未被识别为占位符：{value!r}"
        checked += 1
    assert checked >= 4, f"实际检查到的示例密钥只有 {checked} 个，示例文件结构可能变了"


def test_deploy_script_covers_required_secrets():
    """deploy.sh 必须覆盖全部必检变量，且旧的空转 grep 已经消失。"""
    script = (PROJECT_ROOT / "deploy.sh").read_text(encoding="utf-8")

    for name in ("JWT_SECRET_KEY", "INITIAL_ADMIN_PASSWORD", "SEARXNG_SECRET", "LLM_API_KEY_VAR"):
        assert name in script, f"deploy.sh 未覆盖 {name}"
    # 旧实现只匹配一个仓库里不存在的串，等于完全没校验
    assert "replace_with_32_random_hex_chars" not in script
