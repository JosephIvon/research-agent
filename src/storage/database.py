"""SQLite database for report and task metadata persistence"""
import sqlite3
import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

DATABASE_PATH = os.getenv("DATABASE_PATH", "research_agent.db")

def get_db_path() -> str:
    """Get the database file path, relative to project root if not absolute."""
    if os.path.isabs(DATABASE_PATH):
        return DATABASE_PATH
    # Resolve relative to project root
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(project_root, DATABASE_PATH)


def _get_connection() -> sqlite3.Connection:
    """Get a raw SQLite connection (thread-safe via connection per thread)."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database schema."""
    conn = _get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                user_query TEXT NOT NULL,
                report_content TEXT,
                quality_score REAL,
                quality_grade TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                metadata_json TEXT,
                query_hash TEXT,
                version_number INTEGER DEFAULT 1,
                parent_report_id TEXT,
                change_summary TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                user_query TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                report_id TEXT,
                deliverables_json TEXT
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")

        conn.commit()
    finally:
        conn.close()


class ReportDB:
    """Async-like SQLite database interface for reports and tasks."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or get_db_path()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def save_report(
        self,
        report_id: str,
        task_id: str,
        user_query: str,
        report_content: Optional[str] = None,
        quality_score: Optional[float] = None,
        quality_grade: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Save or update a report record. Returns True if successful."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            now = datetime.now().isoformat(timespec="seconds")
            metadata_json = json.dumps(metadata) if metadata else None

            cursor.execute("""
                INSERT INTO reports (id, task_id, user_query, report_content, quality_score,
                                     quality_grade, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_id = excluded.task_id,
                    user_query = excluded.user_query,
                    report_content = COALESCE(excluded.report_content, reports.report_content),
                    quality_score = excluded.quality_score,
                    quality_grade = excluded.quality_grade,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json
            """, (report_id, task_id, user_query, report_content, quality_score,
                  quality_grade, now, now, metadata_json))
            conn.commit()
            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to save report: {e}")
            return False
        finally:
            conn.close()

    def save_report_version(
        self,
        report_id: str,
        task_id: str,
        user_query: str,
        report_content: Optional[str] = None,
        quality_score: Optional[float] = None,
        quality_grade: Optional[str] = None,
        version_number: int = 1,
        parent_version_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Save a report as a new version."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            now = datetime.now().isoformat(timespec="seconds")
            metadata_json = json.dumps(metadata) if metadata else None

            cursor.execute("""
                INSERT INTO reports (id, task_id, user_query, report_content, quality_score,
                                     quality_grade, created_at, updated_at, metadata_json,
                                     version_number, parent_report_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_id = excluded.task_id,
                    user_query = excluded.user_query,
                    report_content = COALESCE(excluded.report_content, reports.report_content),
                    quality_score = excluded.quality_score,
                    quality_grade = excluded.quality_grade,
                    updated_at = excluded.updated_at,
                    metadata_json = excluded.metadata_json,
                    version_number = excluded.version_number,
                    parent_report_id = excluded.parent_report_id
            """, (report_id, task_id, user_query, report_content, quality_score,
                  quality_grade, now, now, metadata_json, version_number, parent_version_id))
            conn.commit()
            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to save report version: {e}")
            return False
        finally:
            conn.close()

    def get_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        """Get a single report by ID."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def list_reports(self, limit: int = 100) -> List[Dict[str, Any]]:
        """List all reports ordered by created_at descending."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, task_id, user_query, quality_score, quality_grade, created_at
                FROM reports
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def save_task(
        self,
        task_id: str,
        user_query: str,
        status: str,
        report_id: Optional[str] = None,
        deliverables: Optional[Dict[str, bool]] = None,
    ) -> bool:
        """Save or update a task record."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            now = datetime.now().isoformat(timespec="seconds")
            deliverables_json = json.dumps(deliverables) if deliverables else None

            cursor.execute("""
                INSERT INTO tasks (task_id, user_query, status, created_at, updated_at, report_id, deliverables_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    report_id = COALESCE(excluded.report_id, tasks.report_id),
                    deliverables_json = excluded.deliverables_json
            """, (task_id, user_query, status, now, now, report_id, deliverables_json))
            conn.commit()
            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to save task: {e}")
            return False
        finally:
            conn.close()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get a single task by ID."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def update_task_status(self, task_id: str, status: str, report_id: Optional[str] = None) -> bool:
        """Update task status."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            now = datetime.now().isoformat(timespec="seconds")
            if report_id:
                cursor.execute("""
                    UPDATE tasks SET status = ?, updated_at = ?, report_id = ?
                    WHERE task_id = ?
                """, (status, now, report_id, task_id))
            else:
                cursor.execute("""
                    UPDATE tasks SET status = ?, updated_at = ?
                    WHERE task_id = ?
                """, (status, now, task_id))
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to update task status: {e}")
            return False
        finally:
            conn.close()

    def link_report_version(
        self,
        query_hash: str,
        report_id: str,
        parent_report_id: Optional[str] = None,
    ) -> bool:
        """Link report to its version chain."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            now = datetime.now().isoformat(timespec="seconds")

            # Calculate version number
            version_number = 1
            if parent_report_id:
                cursor.execute(
                    "SELECT version_number FROM reports WHERE id = ?",
                    (parent_report_id,)
                )
                row = cursor.fetchone()
                if row:
                    version_number = row["version_number"] + 1

            # Update the report with version info
            cursor.execute("""
                UPDATE reports SET
                    query_hash = ?,
                    version_number = ?,
                    parent_report_id = ?
                WHERE id = ?
            """, (query_hash, version_number, parent_report_id, report_id))
            conn.commit()
            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to link report version: {e}")
            return False
        finally:
            conn.close()

    def get_report_versions(self, report_id: str) -> List[Dict[str, Any]]:
        """Get all versions of a report chain."""
        conn = self._conn()
        try:
            cursor = conn.cursor()

            # Find the query_hash for this report
            cursor.execute(
                "SELECT query_hash FROM reports WHERE id = ?",
                (report_id,)
            )
            row = cursor.fetchone()
            if not row or not row["query_hash"]:
                return []

            query_hash = row["query_hash"]

            # Get all reports with the same query_hash
            cursor.execute("""
                SELECT id, task_id, user_query, quality_score, quality_grade,
                       created_at, version_number, parent_report_id, change_summary
                FROM reports
                WHERE query_hash = ?
                ORDER BY version_number ASC
            """, (query_hash,))

            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_latest_version(self, query_hash: str) -> Optional[Dict[str, Any]]:
        """Get the latest version for a query."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, task_id, user_query, quality_score, quality_grade,
                       created_at, version_number, parent_report_id, change_summary
                FROM reports
                WHERE query_hash = ?
                ORDER BY version_number DESC
                LIMIT 1
            """, (query_hash,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def get_report_by_query_hash(self, query_hash: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Get all reports with the same query hash."""
        conn = self._conn()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, task_id, user_query, quality_score, quality_grade,
                       created_at, version_number, parent_report_id, change_summary
                FROM reports
                WHERE query_hash = ?
                ORDER BY version_number DESC
                LIMIT ?
            """, (query_hash, limit))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()


_report_db: Optional[ReportDB] = None


def get_report_db() -> ReportDB:
    """Get the singleton ReportDB instance, initializing if needed."""
    global _report_db
    if _report_db is None:
        _report_db = ReportDB()
        init_db()
    return _report_db
