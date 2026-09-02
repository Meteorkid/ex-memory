"""用户认证：SQLite + token 简单认证系统。"""

import hashlib
import hmac
import os
import secrets
import time
import logging
from contextlib import contextmanager
from typing import Optional
from pathlib import Path

from core.db import integrity_errors

logger = logging.getLogger("ex-memory")

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "users.db"
# access token 短期、refresh token 长期：
# 只有一种长期 token 时，被盗后的可利用窗口就是它的全部生命周期。
ACCESS_TOKEN_EXPIRY_SECONDS = int(
    os.getenv("ACCESS_TOKEN_EXPIRY_SECONDS", str(2 * 3600))
)
REFRESH_TOKEN_EXPIRY_SECONDS = int(
    os.getenv("REFRESH_TOKEN_EXPIRY_SECONDS", str(30 * 24 * 3600))
)
# 兼容旧引用
TOKEN_EXPIRY_SECONDS = ACCESS_TOKEN_EXPIRY_SECONDS


@contextmanager
def _get_conn():
    """数据库连接上下文管理器。

    实际方言由 core.db 按 DATABASE_URL 决定：配了就走 Postgres 连接池，
    没配就是 SQLite 单文件。业务代码一律按 SQLite 方言写 SQL，翻译在
    core.db 里做——7 个模块都从这里取连接，改一处即可全体切换。
    """
    from core.db import connect

    with connect(DB_PATH) as conn:
        yield conn


def init_db():
    """初始化数据库并运行待执行的迁移。"""
    _run_migrations()


def _get_current_version(conn) -> int:
    """读取当前 schema 版本号。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    # 具名取值：psycopg 用 dict_row，位置访问在两种方言下不通用
    row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    current = row["version"] if row else None
    return int(current) if current is not None else 0


def _run_migrations():
    """按序执行 migrations/ 目录下的 SQL 文件。"""
    from core.db import migration_dir

    migrations_dir = migration_dir()
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

            # 先删后插：INSERT OR REPLACE 是 SQLite 专有的，
            # 两步写法在两种方言下语义一致
            conn.execute("DELETE FROM schema_version WHERE version = ?", (version,))
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS external_identities (
                provider TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (provider, external_user_id),
                UNIQUE (provider, user_id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.commit()


def get_or_create_external_user_id(provider: str, external_user_id: str) -> int:
    """把可信外部身份稳定映射到现有整数用户主键。"""
    provider = provider.strip()
    external_user_id = external_user_id.strip()
    if not provider or not external_user_id:
        raise ValueError("外部身份不能为空")
    if len(provider) > 64 or len(external_user_id) > 255:
        raise ValueError("外部身份过长")

    with _get_conn() as conn:
        # SQLite 的写事务串行化首次创建，避免两个并发请求生成两名影子用户。
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT user_id FROM external_identities
            WHERE provider = ? AND external_user_id = ?
            """,
            (provider, external_user_id),
        ).fetchone()
        if row:
            conn.commit()
            return int(row["user_id"])

        identity_hash = hashlib.sha256(
            f"{provider}:{external_user_id}".encode("utf-8")
        ).hexdigest()[:24]
        username = f"external_{identity_hash}"
        password_hash, salt = _hash_password(secrets.token_urlsafe(32))
        user_id = int(
            conn.insert_returning_id(
                "INSERT INTO users (username, password_hash, salt) VALUES (?, ?, ?)",
                (username, password_hash, salt),
            )
        )
        conn.execute(
            """
            INSERT INTO external_identities (provider, external_user_id, user_id)
            VALUES (?, ?, ?)
            """,
            (provider, external_user_id, user_id),
        )
        conn.commit()
        return user_id


def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
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
        except integrity_errors():
            return "用户名已存在"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _utc_now_str() -> str:
    """当前 UTC 时间串，与 SQLite datetime('now') 口径一致。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _utc_after_str(seconds: int) -> str:
    """seconds 秒之后的 UTC 时间串。"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + seconds))


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

        token, _refresh, _session = _issue_session(conn, int(row["id"]))
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
            return None

        expires = row["expires_at"]
        if expires and expires < _utc_now_str():
            conn.execute("DELETE FROM tokens WHERE token = ?", (token_key,))
            conn.commit()
            return None

        return row["user_id"]


def revoke_token(token: str):
    with _get_conn() as conn:
        token_key = _token_hash(token)
        conn.execute("DELETE FROM tokens WHERE token = ?", (token_key,))
        conn.commit()


def clean_expired_tokens():
    with _get_conn() as conn:
        conn.execute("DELETE FROM tokens WHERE expires_at < datetime('now')")
        conn.commit()


def get_user_role(user_id: int) -> str:
    """读取用户角色。缺省 user——查不到时按最低权限处理。"""
    with _get_conn() as conn:
        row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        return (row["role"] or "user") if row else "user"


def set_user_role(username: str, role: str) -> bool:
    """授予/撤销角色。返回是否命中了用户。"""
    if role not in ("user", "admin"):
        raise ValueError("角色只能是 user 或 admin")
    with _get_conn() as conn:
        cursor = conn.execute(
            "UPDATE users SET role = ? WHERE username = ?", (role, username)
        )
        conn.commit()
        return cursor.rowcount > 0


# ── 会话：access + refresh ──


def _issue_session(
    conn,
    user_id: int,
    session_id: Optional[str] = None,
    user_agent: str = "",
    ip: str = "",
) -> tuple[str, str, str]:
    """签发一对令牌，返回 (access, refresh, session_id)。"""
    session_id = session_id or secrets.token_urlsafe(16)
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO tokens (token, user_id, expires_at, session_id) VALUES (?, ?, ?, ?)",
        (
            _token_hash(access),
            user_id,
            _utc_after_str(ACCESS_TOKEN_EXPIRY_SECONDS),
            session_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO refresh_tokens
            (token, user_id, session_id, expires_at, user_agent, ip)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _token_hash(refresh),
            user_id,
            session_id,
            _utc_after_str(REFRESH_TOKEN_EXPIRY_SECONDS),
            user_agent[:200],
            ip,
        ),
    )
    return access, refresh, session_id


def login_user_with_refresh(
    username: str, password: str, user_agent: str = "", ip: str = ""
) -> Optional[dict]:
    """登录并签发 access + refresh。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, password_hash, salt FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not row:
            return None
        pw_hash, _ = _hash_password(password, row["salt"])
        if not hmac.compare_digest(pw_hash, row["password_hash"]):
            return None
        access, refresh, session_id = _issue_session(
            conn, int(row["id"]), user_agent=user_agent, ip=ip
        )
        conn.commit()
        return {
            "token": access,
            "refresh_token": refresh,
            "session_id": session_id,
            "expires_in": ACCESS_TOKEN_EXPIRY_SECONDS,
        }


def refresh_session(
    refresh_token: str, user_agent: str = "", ip: str = ""
) -> Optional[dict]:
    """用 refresh 换一对新令牌，旧的立即作废（轮换）。

    检测重放：已轮换过的 refresh 再次出现，说明它可能被盗，
    此时吊销整条会话链而不是简单拒绝——盗用方和真实用户都得重新登录。
    """
    key = _token_hash(refresh_token)
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM refresh_tokens WHERE token = ?", (key,)
        ).fetchone()
        if row is None:
            return None

        if row["rotated_to"] is not None:
            conn.execute(
                "UPDATE refresh_tokens SET revoked_at = datetime('now')"
                " WHERE session_id = ? AND revoked_at IS NULL",
                (row["session_id"],),
            )
            conn.execute(
                "DELETE FROM tokens WHERE session_id = ?", (row["session_id"],)
            )
            conn.commit()
            logger.warning(
                "检测到已轮换的 refresh token 被重放，已吊销整条会话 user_id=%s",
                row["user_id"],
            )
            return None

        if row["revoked_at"] is not None or row["expires_at"] < _utc_now_str():
            return None

        user_id = int(row["user_id"])
        session_id = row["session_id"]
        # 旧 access 同时作废，避免一次登录留下多把有效钥匙
        conn.execute("DELETE FROM tokens WHERE session_id = ?", (session_id,))
        access, new_refresh, _ = _issue_session(
            conn, user_id, session_id=session_id, user_agent=user_agent, ip=ip
        )
        conn.execute(
            "UPDATE refresh_tokens SET revoked_at = datetime('now'), rotated_to = ?"
            " WHERE token = ?",
            (_token_hash(new_refresh), key),
        )
        conn.commit()
        return {
            "token": access,
            "refresh_token": new_refresh,
            "session_id": session_id,
            "expires_in": ACCESS_TOKEN_EXPIRY_SECONDS,
        }


def revoke_all_sessions(user_id: int) -> int:
    """登出全部设备。返回吊销的会话数。"""
    with _get_conn() as conn:
        cursor = conn.execute(
            "UPDATE refresh_tokens SET revoked_at = datetime('now')"
            " WHERE user_id = ? AND revoked_at IS NULL",
            (user_id,),
        )
        conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount


def list_sessions(user_id: int) -> list[dict]:
    """当前有效会话，供用户查看「哪些设备登录着」。"""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT session_id, created_at, expires_at, user_agent, ip
            FROM refresh_tokens
            WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
            ORDER BY created_at DESC
            """,
            (user_id, _utc_now_str()),
        ).fetchall()
        return [dict(r) for r in rows]
