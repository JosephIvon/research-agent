"""In-memory user store with SQLite persistence"""
import sqlite3
import json
from datetime import datetime
from typing import Optional, List, Dict
import os
import uuid
from src.auth.jwt_auth import get_password_hash, verify_password, create_access_token
from src.config.settings import INITIAL_ADMIN_USERNAME, INITIAL_ADMIN_PASSWORD


def get_user_db_path():
    path = os.getenv("USER_DB_PATH", "users.db")
    if os.path.isabs(path):
        return path
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    return os.path.join(project_root, path)


def init_user_db():
    conn = sqlite3.connect(get_user_db_path())
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
    conn.close()
    _ensure_initial_admin()


def _ensure_initial_admin():
    if not INITIAL_ADMIN_USERNAME or not INITIAL_ADMIN_PASSWORD:
        return
    conn = sqlite3.connect(get_user_db_path())
    cursor = conn.execute("SELECT id FROM users WHERE username = ?", (INITIAL_ADMIN_USERNAME,))
    if cursor.fetchone():
        conn.close()
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
        pass
    conn.close()

class UserDB:
    def __init__(self):
        self._db_path = get_user_db_path()
        init_user_db()

    def create_user(self, username: str, email: str, password: str, role: str = "user") -> Optional[str]:
        user_id = str(uuid.uuid4())
        password_hash = get_password_hash(password)
        now = datetime.now().isoformat()
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "INSERT INTO users (id, username, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, email, password_hash, role, now)
            )
            conn.commit()
            conn.close()
            return user_id
        except sqlite3.IntegrityError:
            return None

    def authenticate(self, username: str, password: str) -> Optional[str]:
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()
        if row and verify_password(password, row[1]):
            return row[0]
        return None

    def get_user(self, user_id: str) -> Optional[Dict]:
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute("SELECT id, username, email, role, created_at, last_login FROM users WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"id": row[0], "username": row[1], "email": row[2], "role": row[3], "created_at": row[4], "last_login": row[5]}
        return None

_user_db: Optional[UserDB] = None
def get_user_db() -> UserDB:
    global _user_db
    if _user_db is None:
        _user_db = UserDB()
    return _user_db