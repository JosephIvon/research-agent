"""SQLite schema migrations.

刻意不引入 Alembic：当前只有 SQLite + 一张业务库，引入额外框架的成本高于收益。
迁移必须幂等——靠 `PRAGMA table_info` 探测列是否存在，而不是记录版本号，
这样「执行两次」与「执行一次」结果相同，也不需要维护 schema_version 表。
"""
import sqlite3
from typing import Optional, Set

logger = __import__("logging").getLogger(__name__)


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """把历史库升级到当前 schema。可重复执行。"""
    if "user_id" not in _columns(conn, "reports"):
        conn.execute("ALTER TABLE reports ADD COLUMN user_id TEXT")
    if "user_id" not in _columns(conn, "tasks"):
        conn.execute("ALTER TABLE tasks ADD COLUMN user_id TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_user ON reports(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)")
    conn.commit()


def backfill_owner_users(conn: sqlite3.Connection, admin_user_id: Optional[str]) -> int:
    """把存量无主数据归到初始管理员。

    `admin_user_id` 为 None 时**保持 NULL 不动**——NULL 表示"无主"，
    这类数据按策略仅 admin 可见。刻意不虚构 `legacy` 用户：那会破坏外键完整性，
    并让「这份报告归谁」的查询返回一个查无此人的 id。

    返回被回填的 reports 行数。
    """
    if not admin_user_id:
        return 0
    try:
        cur = conn.execute(
            "UPDATE reports SET user_id = ? WHERE user_id IS NULL", (admin_user_id,)
        )
        report_rows = cur.rowcount or 0
        conn.execute(
            "UPDATE tasks SET user_id = ? WHERE user_id IS NULL", (admin_user_id,)
        )
        conn.commit()
        if report_rows:
            logger.info(
                "已把 %d 条存量报告回填到初始管理员 %s", report_rows, admin_user_id
            )
        return report_rows
    except sqlite3.Error as exc:
        logger.warning("存量数据归属回填失败（不影响启动）: %s", exc)
        return 0