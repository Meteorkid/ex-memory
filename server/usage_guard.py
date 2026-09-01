"""使用强度保护（FR-023）。

原先 HealthTracker 把状态放在进程内字典，重启清零、多设备各算各的、
多副本下彻底错乱。这里改为按「用户 + 自然日」落 user_activity 表，
跨设备与跨进程一致。

🔴 与危机干预的优先级：危机识别排在本闸门之前，达到时长上限的用户
仍然能拿到危机响应。任何情况下求助信息都必须送达。
"""

import logging
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ex-memory")

# 单次请求最多计入的时长，避免长时间挂机后一次性灌进大量时长
_MAX_INCREMENT_SECONDS = 300
_FRESH_INTERACTION_SECONDS = 30


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_ts() -> float:
    return time.time()


def _parse_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def touch(user_id: int) -> dict:
    """记录一次活跃并返回当日状态。

    两次请求间隔在阈值内按连续使用累计，超出则按一次新交互计固定秒数——
    否则用户中午聊两句、晚上再聊两句，中间几小时会被算成使用时长。
    """
    import config
    from server.auth import _get_conn

    now = _now_ts()
    today = _today()
    now_iso = datetime.now().isoformat()

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT active_seconds, last_active_at, cooldown_until"
            " FROM user_activity WHERE user_id = ? AND activity_date = ?",
            (user_id, today),
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO user_activity (user_id, activity_date, active_seconds,"
                " last_active_at) VALUES (?, ?, ?, ?)",
                (user_id, today, _FRESH_INTERACTION_SECONDS, now_iso),
            )
            conn.commit()
            return {
                "active_seconds": _FRESH_INTERACTION_SECONDS,
                "over_limit": False,
                "cooldown_until": None,
            }

        last = _parse_ts(row["last_active_at"])
        gap = now - last if last else None
        if gap is not None and 0 <= gap <= config.USAGE_GAP_THRESHOLD_SECONDS:
            increment = min(gap, _MAX_INCREMENT_SECONDS)
        else:
            increment = _FRESH_INTERACTION_SECONDS

        active = int(row["active_seconds"] or 0) + int(increment)
        cooldown_until = row["cooldown_until"]

        over = active >= config.DAILY_USAGE_LIMIT_SECONDS
        if over and not cooldown_until:
            cooldown_until = datetime.fromtimestamp(
                now + config.USAGE_COOLDOWN_SECONDS
            ).isoformat()
            logger.info("用户达到当日使用上限，进入冷静期 user=%s", user_id)

        conn.execute(
            "UPDATE user_activity SET active_seconds = ?, last_active_at = ?,"
            " cooldown_until = ? WHERE user_id = ? AND activity_date = ?",
            (active, now_iso, cooldown_until, user_id, today),
        )
        conn.commit()

    return {
        "active_seconds": active,
        "over_limit": over,
        "cooldown_until": cooldown_until,
    }


def status(user_id: int) -> dict:
    """只读查询当日状态，不计入时长。"""
    import config
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT active_seconds, cooldown_until FROM user_activity"
            " WHERE user_id = ? AND activity_date = ?",
            (user_id, _today()),
        ).fetchone()

    active = int(row["active_seconds"]) if row else 0
    cooldown_until = row["cooldown_until"] if row else None
    return {
        "active_seconds": active,
        "active_minutes": round(active / 60, 1),
        "daily_limit_seconds": config.DAILY_USAGE_LIMIT_SECONDS,
        "cooldown_until": cooldown_until,
        "in_cooldown": _in_cooldown(cooldown_until),
    }


def _in_cooldown(cooldown_until: Optional[str]) -> bool:
    ts = _parse_ts(cooldown_until)
    return ts is not None and _now_ts() < ts


def check_limit(user_id: int) -> Optional[dict]:
    """达到上限且仍在冷静期时返回提示，否则返回 None。

    调用方必须把它排在危机识别之后——正在求救的人不能被时长上限挡住。
    """
    state = touch(user_id)
    if not _in_cooldown(state.get("cooldown_until")):
        return None
    return {
        "type": "usage_limit",
        "message": (
            "今天聊了挺久了，先歇一会儿吧。\n回忆随时都在，但此刻的生活也需要你。"
        ),
    }
