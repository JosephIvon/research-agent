"""缓存生命周期清理入口（CLI + 环境默认值）。

默认策略：
  - completed / cancelled / partial：终态后保留 7 天
  - failed：保留 30 天（留出复盘窗口）
  - 全局上界：30 万行 + 2 GiB（双限，超限时按任务更新时间最旧优先淘汰）
  - running / pending：永不清理；无任务行的缓存永不清理

用法：
  uv run python -m src.storage.prune                 # 干跑，只打印将删除的行数/字节数
  uv run python -m src.storage.prune --apply         # 实际删除
  uv run python -m src.storage.prune --db other.db --completed-days 3 --max-rows 10000
"""
import argparse
import json
import os
import sys


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def prune_from_env(dry_run: bool = True) -> dict:
    """按环境变量清理；供 CLI 与启动钩子共用。"""
    from src.storage.database import get_report_db

    return get_report_db().prune_caches(
        dry_run=dry_run,
        completed_keep_days=_env_int("CACHE_COMPLETED_KEEP_DAYS", 7),
        failed_keep_days=_env_int("CACHE_FAILED_KEEP_DAYS", 30),
        max_rows=_env_int("CACHE_MAX_ROWS", 300_000),
        max_bytes=_env_int("CACHE_MAX_BYTES", 2 * 1024 ** 3),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="清理 page_cache / task_drafts")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="实际删除")
    mode.add_argument("--dry-run", action="store_true", help="只报告将删除的行数/字节数（默认）")
    parser.add_argument("--db", default="", help="数据库路径（缺省用 DATABASE_PATH）")
    parser.add_argument("--completed-days", type=int, default=None)
    parser.add_argument("--failed-days", type=int, default=None)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--max-bytes", type=int, default=None)
    parser.add_argument("--init", action="store_true", help="先建表再清理")
    args = parser.parse_args(argv)

    if args.db:
        os.environ["DATABASE_PATH"] = args.db

    from src.storage import database as dbmod

    if args.db:
        dbmod.DATABASE_PATH = args.db
    if args.init:
        dbmod.init_db()

    kwargs = {}
    if args.completed_days is not None:
        kwargs["completed_keep_days"] = args.completed_days
    if args.failed_days is not None:
        kwargs["failed_keep_days"] = args.failed_days
    if args.max_rows is not None:
        kwargs["max_rows"] = args.max_rows
    if args.max_bytes is not None:
        kwargs["max_bytes"] = args.max_bytes

    summary = dbmod.get_report_db().prune_caches(dry_run=not args.apply, **kwargs)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
