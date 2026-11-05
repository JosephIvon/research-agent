"""启动配置自检（fail-fast）。

只拒绝会互相矛盾、导致静默错误结果的开关组合；不改变任何默认值的语义。
总开关：`STARTUP_CONFIG_CHECKS_ENABLED`（默认 1，置 0 跳过全部检查，供排障）。
"""
import logging
import os
from typing import List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

CHECK_SWITCH = "STARTUP_CONFIG_CHECKS_ENABLED"
# 显式的副本/worker 数配置。当前没有分布式租约，只能靠部署方如实声明。
REPLICA_ENV_VARS = (
    "WEB_CONCURRENCY",
    "UVICORN_WORKERS",
    "APP_WORKERS",
    "LIFECYCLE_RECOVER_REPLICAS",
)
# 启动清理的下限：低于这两个值视为“明显过小”，会一次性淘汰大量终态缓存。
MIN_CACHE_ROWS = 1000
MIN_CACHE_BYTES = 100 * 1024 ** 2


class StartupConfigError(RuntimeError):
    """启动配置矛盾。消息里逐条包含变量名与修法。"""


def _int_or_none(env: Mapping[str, str], name: str) -> Optional[int]:
    raw = env.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        # 非法值由下游按默认值兜底；这里不重复报错。
        return None


def _explicit_replica_counts(env: Mapping[str, str]) -> List[Tuple[str, int]]:
    counts: List[Tuple[str, int]] = []
    for name in REPLICA_ENV_VARS:
        value = _int_or_none(env, name)
        if value is not None and value > 1:
            counts.append((name, value))
    return counts


def validate_startup_config(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """校验启动配置；返回需要打印的告警，矛盾时抛 `StartupConfigError`。

    规则：
      1. 自动恢复开启但检查点关闭：崩溃后的 running 会因无检查点被判 failed。
      2. 自动恢复开启且显式声明多副本/多 worker：没有租约，会重复恢复同一任务。
         探测不到副本数时只能告警，明确把责任交给部署方。
      3. 启动清理开启但容量上界明显过小：会一次性淘汰大量终态缓存。
    """
    env = os.environ if env is None else env
    if env.get(CHECK_SWITCH, "1") != "1":
        logger.info("启动配置自检已关闭（%s != 1）", CHECK_SWITCH)
        return []

    errors: List[str] = []
    warnings: List[str] = []

    recovery_on = env.get("LIFECYCLE_RECOVER_ON_STARTUP", "0") == "1"
    if recovery_on and env.get("LIFECYCLE_CHECKPOINTS", "1") == "0":
        errors.append(
            "LIFECYCLE_RECOVER_ON_STARTUP=1 与 LIFECYCLE_CHECKPOINTS=0 矛盾："
            "新任务不会写阶段检查点，进程崩溃后残留的 running 行会被启动恢复标记为 "
            "failed（reason_code=no_checkpoint），自动恢复变成自动判死。"
            "修法：二选一——关闭自动恢复（LIFECYCLE_RECOVER_ON_STARTUP=0），"
            "或打开阶段检查点（LIFECYCLE_CHECKPOINTS=1）。"
        )

    if recovery_on:
        replicas = _explicit_replica_counts(env)
        if replicas:
            details = "、".join(f"{name}={value}" for name, value in replicas)
            errors.append(
                f"LIFECYCLE_RECOVER_ON_STARTUP=1 与显式多副本配置冲突（{details}）："
                "当前没有分布式租约，多进程共享同一 DATABASE_PATH 会重复恢复同一任务，"
                "造成重复抓取、重复计费和重复写报告。"
                "修法：多副本部署必须关闭自动恢复（LIFECYCLE_RECOVER_ON_STARTUP=0）；"
                "只有单实例才可置 1。"
            )
        else:
            warnings.append(
                "启动配置自检：无法探测副本数/worker 数（未设置 "
                + " / ".join(REPLICA_ENV_VARS)
                + "）。责任在部署方：LIFECYCLE_RECOVER_ON_STARTUP=1 只允许单实例运行；"
                "多副本共享同一 DATABASE_PATH 会重复恢复同一任务。"
                "若不是单实例，请置 LIFECYCLE_RECOVER_ON_STARTUP=0。"
            )

    prune_on_startup = (
        env.get("CACHE_PRUNE_ENABLED", "1") == "1"
        and env.get("CACHE_PRUNE_ON_STARTUP", "0") == "1"
    )
    if prune_on_startup:
        rows = _int_or_none(env, "CACHE_MAX_ROWS")
        if rows is not None and rows < MIN_CACHE_ROWS:
            errors.append(
                f"CACHE_PRUNE_ON_STARTUP=1 但 CACHE_MAX_ROWS={rows} 明显过小"
                f"（下限 {MIN_CACHE_ROWS} 行）：启动清理会一次性淘汰大量终态缓存，"
                "报告与事件不丢，但复盘用的原始页与草稿会被删掉。"
                f"修法：把 CACHE_MAX_ROWS 调到 {MIN_CACHE_ROWS} 以上，"
                "或关闭启动清理（CACHE_PRUNE_ON_STARTUP=0）。"
            )
        max_bytes = _int_or_none(env, "CACHE_MAX_BYTES")
        if max_bytes is not None and max_bytes < MIN_CACHE_BYTES:
            errors.append(
                f"CACHE_PRUNE_ON_STARTUP=1 但 CACHE_MAX_BYTES={max_bytes} 明显过小"
                f"（下限 {MIN_CACHE_BYTES} 字节 = 100 MiB）：启动清理会一次性淘汰大量"
                "终态缓存，复盘用的原始页与草稿会被删掉。"
                f"修法：把 CACHE_MAX_BYTES 调到 {MIN_CACHE_BYTES} 以上，"
                "或关闭启动清理（CACHE_PRUNE_ON_STARTUP=0）。"
            )

    if errors:
        raise StartupConfigError("启动配置自检失败，拒绝启动：\n- " + "\n- ".join(errors))
    return warnings
