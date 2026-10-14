"""In-memory user store with SQLite persistence"""
import logging
import sqlite3
import json
from datetime import datetime
from typing import Optional, List, Dict
import os
import uuid
from src.auth.jwt_auth import get_password_hash, verify_password, create_access_token
from src.config.settings import (
    INITIAL_ADMIN_USERNAME,
    INITIAL_ADMIN_PASSWORD,
    MIN_PASSWORD_LENGTH,
    describe_weak_secret,
    is_weak_secret,
)

logger = logging.getLogger(__name__)


def get_user_db_path():
    path = os.getenv("USER_DB_PATH", "users.db")
    if os.path.isabs(path):
        return path
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(project_root, path)


def _connect():
    return sqlite3.connect(get_user_db_path())


def init_user_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                created_at TEXT NOT NULL,
                last_login TEXT
            )
        """)
        conn.commit()
    _ensure_initial_admin()


def _ensure_initial_admin():
    if not INITIAL_ADMIN_USERNAME or not INITIAL_ADMIN_PASSWORD:
        return
    # 占位符/过短口令直接放弃建号：绝不让 .env.example 的示例口令经 bcrypt 变成可用管理员。
    # 判据来自 src.config.settings，与 deploy.sh 的表驱动校验同源。
    if is_weak_secret(INITIAL_ADMIN_PASSWORD, MIN_PASSWORD_LENGTH):
        logger.warning(
            "跳过初始管理员 %r 创建：%s",
            INITIAL_ADMIN_USERNAME,
            describe_weak_secret("INITIAL_ADMIN_PASSWORD", INITIAL_ADMIN_PASSWORD, MIN_PASSWORD_LENGTH),
        )
        return
    with _connect() as conn:
        cursor = conn.execute(
            "SELECT id FROM users WHERE username = ?", (INITIAL_ADMIN_USERNAME,)
        )
        if cursor.fetchone():
            return
        user_id = str(uuid.uuid4())
        password_hash = get_password_hash(INITIAL_ADMIN_PASSWORD)
        now = datetime.now().isoformat()
        try:
            conn.execute(
                "INSERT INTO users (id, username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, INITIAL_ADMIN_USERNAME, f"{INITIAL_ADMIN_USERNAME}@local", password_hash, "admin", now)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 并发下另一个进程已创建同名管理员
            pass

class UserDB:
    def __init__(self):
        self._db_path = get_user_db_path()
        init_user_db()

    def create_user(self, username: str, email: str, password: str, role: str = "user") -> Optional[str]:
        user_id = str(uuid.uuid4())
        password_hash = get_password_hash(password)
        now = datetime.now().isoformat()
        try:
            # 上下文管理器保证异常路径下连接也被关闭（此前 IntegrityError 分支直接 return，连接泄漏）
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO users (id, username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, username, email, password_hash, role, now)
                )
                conn.commit()
            return user_id
        except sqlite3.IntegrityError:
            return None

    def authenticate(self, username: str, password: str) -> Optional[Dict]:
        """校验口令，成功返回 {'id', 'role'}，失败返回 None。"""
        with _connect() as conn:
            cursor = conn.execute(
                "SELECT id, password_hash, role FROM users WHERE username = ?", (username,)
            )
            row = cursor.fetchone()
        if row and verify_password(password, row[1]):
            return {"id": row[0], "role": row[2] or "user"}
        return None

    def get_user_by_username(self, username: str) -> Optional[Dict]:
        with _connect() as conn:
            cursor = conn.execute(
                "SELECT id, username, email, role, created_at, last_login FROM users WHERE username = ?",
                (username,),
            )
            row = cursor.fetchone()
        if row:
            return {"id": row[0], "username": row[1], "email": row[2], "role": row[3], "created_at": row[4], "last_login": row[5]}
        return None

    def get_user(self, user_id: str) -> Optional[Dict]:
        with _connect() as conn:
            cursor = conn.execute(
                "SELECT id, username, email, role, created_at, last_login FROM users WHERE id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
        if row:
            return {"id": row[0], "username": row[1], "email": row[2], "role": row[3], "created_at": row[4], "last_login": row[5]}
        return None

    def is_admin(self, user_id: Optional[str]) -> bool:
        """是否管理员。user_id 为空或查不到时一律返回 False。"""
        if not user_id:
            return False
        user = self.get_user(user_id)
        return bool(user) and user.get("role") == "admin"

_user_db: Optional[UserDB] = None
def get_user_db() -> UserDB:
    global _user_db
    if _user_db is None:
        _user_db = UserDB()
    return _user_db