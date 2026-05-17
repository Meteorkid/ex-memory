"""用户认证：SQLite + token 简单认证系统。"""

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

logger = logging.getLogger("ex-memory")

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "users.db"
TOKEN_EXPIRY_SECONDS = 7 * 24 * 3600  # 7 天


@contextmanager
def _get_conn():
    """数据库连接上下文管理器，确保连接总是被关闭。"""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """初始化数据库并运行待执行的迁移。"""
    _run_migrations()


def _get_current_version(conn) -> int:
    """读取当前 schema 版本号。"""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] if row[0] is not None else 0


def _run_migrations():
    """按序执行 migrations/ 目录下的 SQL 文件。"""
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    if not migrations_dir.exists():
        # 兜底：直接建表
        _bootstrap_tables()
        return

    with _get_conn() as conn:
        current_version = _get_current_version(conn)

        for sql_file in sorted(migrations_dir.glob("*.sql")):
            # 从文件名提取版本号（如 001_init.sql → 1）
            try:
                version = int(sql_file.stem.split("_")[0])
            except ValueError:
                continue

            if version <= current_version:
                continue

            logger.info("运行数据库迁移: %s", sql_file.name)
            sql = sql_file.read_text(encoding="utf-8")
            conn.executescript(sql)

            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (version,),
            )
            conn.commit()


def _bootstrap_tables():
    """兜底建表（migrations 目录不存在时使用）。"""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                expires_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.commit()


def _hash_password(password: str, salt: str = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000)
    return h.hex(), salt


def register_user(username: str, password: str) -> Optional[str]:
    """注册新用户，返回错误信息或 None。"""
    if len(username) < 2:
        return "用户名至少 2 个字符"
    if len(password) < 6:
        return "密码至少 6 个字符"

    with _get_conn() as conn:
        try:
            pw_hash, salt = _hash_password(password)
            conn.execute(
                "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
                (username, pw_hash, salt),
            )
            conn.commit()
            return None
        except sqlite3.IntegrityError:
            return "用户名已存在"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login_user(username: str, password: str) -> Optional[str]:
    """验证登录，返回 token 或 None。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, password_hash, salt FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if not row:
            return None

        pw_hash, _ = _hash_password(password, row["salt"])
        if not hmac.compare_digest(pw_hash, row["password_hash"]):
            return None

        # 生成 token
        token = secrets.token_urlsafe(32)
        expires = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + TOKEN_EXPIRY_SECONDS))
        conn.execute(
            "INSERT INTO tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), row["id"], expires),
        )
        conn.commit()
        return token


def validate_token(token: str) -> Optional[int]:
    """验证 token，返回 user_id 或 None。"""
    with _get_conn() as conn:
        token_key = _token_hash(token)
        row = conn.execute(
            "SELECT user_id, expires_at FROM tokens WHERE token = ?",
            (token_key,),
        ).fetchone()

        if not row:
            row = conn.execute(
                "SELECT user_id, expires_at FROM tokens WHERE token = ?",
                (token,),
            ).fetchone()

        if not row:
            return None

        expires = row["expires_at"]
        if expires and expires < time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()):
            conn.execute("DELETE FROM tokens WHERE token IN (?, ?)", (token_key, token))
            conn.commit()
            return None

        return row["user_id"]


def revoke_token(token: str):
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM tokens WHERE token IN (?, ?)",
            (_token_hash(token), token),
        )
        conn.commit()


def clean_expired_tokens():
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM tokens WHERE expires_at < datetime('now')"
        )
        conn.commit()


# 初始化数据库
init_db()
