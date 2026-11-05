"""启动配置自检：矛盾组合必须拒绝启动，提示含变量名与修法。"""
import asyncio

import pytest

from src.config import startup_checks as sc


def _env(**kwargs) -> dict:
    return {key: str(value) for key, value in kwargs.items()}


def test_checkpoints_conflict_rejected():
    env = _env(LIFECYCLE_RECOVER_ON_STARTUP=1, LIFECYCLE_CHECKPOINTS=0)
    with pytest.raises(sc.StartupConfigError) as exc:
        sc.validate_startup_config(env)
    message = str(exc.value)
    assert "LIFECYCLE_RECOVER_ON_STARTUP" in message
    assert "LIFECYCLE_CHECKPOINTS" in message
    assert "no_checkpoint" in message
    assert "修法" in message


@pytest.mark.parametrize("worker_var", sc.REPLICA_ENV_VARS)
def test_multi_worker_conflict_rejected(worker_var):
    env = _env(
        LIFECYCLE_RECOVER_ON_STARTUP=1,
        LIFECYCLE_CHECKPOINTS=1,
        **{worker_var: 2},
    )
    with pytest.raises(sc.StartupConfigError) as exc:
        sc.validate_startup_config(env)
    message = str(exc.value)
    assert worker_var in message
    assert "LIFECYCLE_RECOVER_ON_STARTUP" in message
    assert "修法" in message


def test_replica_count_undetectable_is_explicit_warning():
    env = _env(LIFECYCLE_RECOVER_ON_STARTUP=1, LIFECYCLE_CHECKPOINTS=1)
    warnings = sc.validate_startup_config(env)
    assert len(warnings) == 1
    assert "无法探测" in warnings[0]
    assert "责任在部署方" in warnings[0]


def test_replica_check_only_when_recovery_enabled():
    env = _env(LIFECYCLE_RECOVER_ON_STARTUP=0, WEB_CONCURRENCY=2)
    assert sc.validate_startup_config(env) == []


def test_prune_small_row_cap_rejected():
    env = _env(CACHE_PRUNE_ON_STARTUP=1, CACHE_MAX_ROWS=999)
    with pytest.raises(sc.StartupConfigError) as exc:
        sc.validate_startup_config(env)
    message = str(exc.value)
    assert "CACHE_MAX_ROWS" in message
    assert str(sc.MIN_CACHE_ROWS) in message
    assert "修法" in message


def test_prune_small_byte_cap_rejected():
    env = _env(CACHE_PRUNE_ON_STARTUP=1, CACHE_MAX_BYTES=sc.MIN_CACHE_BYTES - 1)
    with pytest.raises(sc.StartupConfigError) as exc:
        sc.validate_startup_config(env)
    message = str(exc.value)
    assert "CACHE_MAX_BYTES" in message
    assert "100 MiB" in message
    assert "修法" in message


def test_prune_thresholds_at_lower_bound_allowed():
    env = _env(
        CACHE_PRUNE_ON_STARTUP=1,
        CACHE_MAX_ROWS=sc.MIN_CACHE_ROWS,
        CACHE_MAX_BYTES=sc.MIN_CACHE_BYTES,
    )
    assert sc.validate_startup_config(env) == []


def test_default_config_passes():
    assert sc.validate_startup_config({}) == []


def test_master_switch_disables_all_checks():
    env = _env(
        STARTUP_CONFIG_CHECKS_ENABLED=0,
        LIFECYCLE_RECOVER_ON_STARTUP=1,
        LIFECYCLE_CHECKPOINTS=0,
        CACHE_PRUNE_ON_STARTUP=1,
        CACHE_MAX_ROWS=1,
    )
    assert sc.validate_startup_config(env) == []


def test_prune_check_skipped_when_cache_prune_disabled():
    env = _env(
        CACHE_PRUNE_ENABLED=0,
        CACHE_PRUNE_ON_STARTUP=1,
        CACHE_MAX_ROWS=1,
    )
    assert sc.validate_startup_config(env) == []


def test_startup_hook_rejects_conflict(monkeypatch, tmp_path):
    monkeypatch.setenv("LIFECYCLE_RECOVER_ON_STARTUP", "1")
    monkeypatch.setenv("LIFECYCLE_CHECKPOINTS", "0")
    # 隔离数据库：即使检查被误删，测试也只碰临时库，不污染仓库内的 reports.db。
    import src.storage.database as dbmod
    monkeypatch.setattr(dbmod, "DATABASE_PATH", str(tmp_path / "reports.db"))
    monkeypatch.setattr(dbmod, "_report_db", None)

    import server

    with pytest.raises(sc.StartupConfigError) as exc:
        asyncio.run(server._recover_running_tasks())
    message = str(exc.value)
    assert "LIFECYCLE_RECOVER_ON_STARTUP" in message
    assert "LIFECYCLE_CHECKPOINTS" in message
    assert "修法" in message
