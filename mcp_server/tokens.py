"""MCP 令牌的签发、校验与吊销。

与 server/auth.py 的会话令牌刻意分开：
- 会话令牌 2 小时过期，服务于浏览器登录；
- MCP 令牌默认长期有效，服务于跑在本机的 MCP 客户端，
  且必须能单独吊销——客户端配置散落在 Claude Desktop、Cursor、
  AstrBot 各处，撤销一次登录不该把所有客户端一起踢下线。

只存 SHA-256 哈希，明文仅在签发时返回一次。
"""

import hashlib
import logging
import secrets
from typing import Optional

logger = logging.getLogger("ex-memory")

PREFIX = "exm"
DEFAULT_TTL_DAYS = 365


def _utc_now_str() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _utc_after_str(seconds: int) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + seconds))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(user_id: int, label: str = "", ttl_days: Optional[int] = None) -> str:
    """签发一枚 MCP 令牌，返回明文（仅此一次）。"""
    secret = secrets.token_urlsafe(32)
    token = f"{PREFIX}_{secret}"
    days = DEFAULT_TTL_DAYS if ttl_days is None else ttl_days
    expires_at = _utc_after_str(days * 86400) if days > 0 else None

    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO mcp_tokens (token, user_id, label, prefix, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (_hash(token), user_id, label[:64], token[:12], expires_at),
        )
        conn.commit()
    return token


def validate(token: str) -> Optional[int]:
    """校验令牌，返回 user_id；无效、过期或已吊销返回 None。"""
    if not token:
        return None

    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at, revoked_at FROM mcp_tokens WHERE token = ?",
            (_hash(token),),
        ).fetchone()

        if not row:
            return None
        if row["revoked_at"]:
            return None

        expires = row["expires_at"]
        now = _utc_now_str()
        if expires and expires <= now:
            return None

        # 每次校验顺带刷新使用时间，便于判断哪些令牌还活着
        try:
            conn.execute(
                "UPDATE mcp_tokens SET last_used_at = ? WHERE token = ?",
                (now, _hash(token)),
            )
            conn.commit()
        except Exception as e:  # 仅用于观测，不能因它拒绝合法请求
            logger.debug("更新令牌使用时间失败: %s", e)

        return int(row["user_id"])


def revoke(prefix: str, user_id: Optional[int] = None) -> int:
    """按前缀吊销令牌。返回受影响行数。

    前缀即令牌前 12 个字符（如 `exm_a1b2c3d4`），签发时展示给用户，
    足以定位一枚令牌，又短到可以安全地贴在 issue 记录里。
    """
    from server.auth import _get_conn

    with _get_conn() as conn:
        if user_id is None:
            cur = conn.execute(
                "UPDATE mcp_tokens SET revoked_at = ? WHERE prefix = ? AND revoked_at IS NULL",
                (_utc_now_str(), prefix),
            )
        else:
            cur = conn.execute(
                """
                UPDATE mcp_tokens SET revoked_at = ?
                 WHERE prefix = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (_utc_now_str(), prefix, user_id),
            )
        conn.commit()
        return cur.rowcount if cur.rowcount is not None else 0


def list_tokens(user_id: int) -> list[dict]:
    """列出某用户的全部 MCP 令牌（不含明文）。"""
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT prefix, label, created_at, last_used_at, expires_at, revoked_at
              FROM mcp_tokens WHERE user_id = ? ORDER BY created_at
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]
