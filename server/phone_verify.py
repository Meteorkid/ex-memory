"""手机号验证码的签发与校验（FR-020）。

验证码存内存：M0 阶段单进程部署，且它是 5 分钟即失效的短命数据。
M1 迁 Redis 时这里要一并搬走——多副本下内存态会直接失效。
"""

import hashlib
import hmac
import logging
import re
import threading
import time
from typing import Optional

logger = logging.getLogger("ex-memory")

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
MAX_ATTEMPTS = 5
RESEND_INTERVAL_SECONDS = 60

# 验证码走共享 KV：多副本下若存在进程内，换台机器验证就失效。
# TTL 由 KV 负责，不需要自己清理。
_lock = threading.Lock()


def _code_key(phone: str) -> str:
    return f"sms:code:{phone}"


def _attempt_key(phone: str) -> str:
    return f"sms:attempts:{phone}"


def _issued_key(phone: str) -> str:
    return f"sms:issued:{phone}"


def normalize_phone(phone: str) -> str:
    phone = (phone or "").strip().replace(" ", "").replace("-", "")
    if not PHONE_RE.match(phone):
        raise ValueError("手机号格式不正确")
    return phone


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def issue_code(phone: str) -> tuple[bool, str]:
    """签发验证码，返回 (是否成功, 提示)。"""
    from core.safety.sms import CODE_TTL_SECONDS, generate_code, send_code

    from core import kv

    phone = normalize_phone(phone)
    issued_at = kv.get(_issued_key(phone))
    if issued_at:
        elapsed = time.time() - float(issued_at)
        if elapsed < RESEND_INTERVAL_SECONDS:
            return False, f"请 {int(RESEND_INTERVAL_SECONDS - elapsed)} 秒后再试"

    code = generate_code()
    if not send_code(phone, code):
        return False, "验证码发送失败，请稍后重试"

    kv.set(_code_key(phone), _hash(code), ttl_seconds=CODE_TTL_SECONDS)
    kv.set(_issued_key(phone), str(time.time()), ttl_seconds=RESEND_INTERVAL_SECONDS)
    kv.delete(_attempt_key(phone))
    return True, "验证码已发送"


def verify_code(phone: str, code: str) -> bool:
    """校验验证码。成功后立即失效，防止复用。"""
    try:
        phone = normalize_phone(phone)
    except ValueError:
        return False

    from core import kv
    from core.safety.sms import CODE_TTL_SECONDS

    with _lock:
        stored = kv.get(_code_key(phone))
        if stored is None:
            return False  # 不存在或已过期，TTL 由 KV 负责
        attempts = kv.incr_by(_attempt_key(phone), 1, ttl_seconds=CODE_TTL_SECONDS)
        if attempts > MAX_ATTEMPTS:
            # 暴力尝试直接作废，而不是继续给机会
            kv.delete(_code_key(phone))
            logger.warning("验证码尝试次数超限，已作废 phone=%s", phone[:3] + "****")
            return False
        if hmac.compare_digest(stored, _hash(code or "")):
            kv.delete(_code_key(phone))
            kv.delete(_attempt_key(phone))
            return True
        return False


def reset() -> None:
    """测试用：验证码状态随 KV 一起重置。"""
    from core import kv

    kv.reset_for_tests()


def phone_in_use(phone: str) -> bool:
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE phone = ? LIMIT 1", (phone,)
        ).fetchone()
        return row is not None


def bind_phone(user_id: int, phone: str) -> None:
    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET phone = ?, phone_verified_at = datetime('now')"
            " WHERE id = ?",
            (phone, user_id),
        )
        conn.commit()


def mark_age_confirmed(user_id: int) -> None:
    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET age_confirmed_at = datetime('now') WHERE id = ?",
            (user_id,),
        )
        conn.commit()


def get_user_id_by_username(username: str) -> Optional[int]:
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        return int(row["id"]) if row else None
