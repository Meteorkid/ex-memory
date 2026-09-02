"""主动发起对话（FR-071 / FR-072）。

这是让 ta「活着」最关键的一项。改造前完全被动：用户不说话就永远沉默。
真实关系里「ta 突然发来一条消息」的情绪冲击远大于「我问 ta 答」——
一个永远等你先开口的对象，本质上是个工具而不是个人。

🔴 **健康保护优先于一切主动性**（FR-072）：处于危机干预冷静期、或已达当日
使用时长上限的用户，一条主动消息都不推。这个产品的用户群本就有沉溺风险，
主动召回是加剧沉溺最直接的手段，必须让位于保护。

默认关闭。主动消息是打扰，得用户明确开启。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger("ex-memory")

TRIGGER_MORNING = "morning"
TRIGGER_NIGHT = "night"
TRIGGER_SILENCE = "silence"
TRIGGER_ANNIVERSARY = "anniversary"

# 沉默多久才算「好久没聊」
SILENCE_THRESHOLD_HOURS = 48


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exe_key(slug: str, owner: Optional[int]) -> str:
    from core.relationship import exe_key

    return exe_key(slug, owner)


# ── 配置 ──


def get_config(slug: str, owner: Optional[int] = None) -> dict:
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT enabled, max_per_day, quiet_start, quiet_end"
            " FROM proactive_configs WHERE exe_key = ?",
            (_exe_key(slug, owner),),
        ).fetchone()
    if row is None:
        # 默认关闭：主动消息是打扰，得用户明确开启
        return {"enabled": False, "max_per_day": 2, "quiet_start": 23, "quiet_end": 8}
    return {
        "enabled": bool(row["enabled"]),
        "max_per_day": int(row["max_per_day"]),
        "quiet_start": int(row["quiet_start"]),
        "quiet_end": int(row["quiet_end"]),
    }


def set_config(
    slug: str,
    *,
    owner: Optional[int] = None,
    enabled: Optional[bool] = None,
    max_per_day: Optional[int] = None,
    quiet_start: Optional[int] = None,
    quiet_end: Optional[int] = None,
) -> dict:
    from server.auth import _get_conn

    current = get_config(slug, owner)
    merged = {
        "enabled": current["enabled"] if enabled is None else bool(enabled),
        "max_per_day": current["max_per_day"] if max_per_day is None else max_per_day,
        "quiet_start": current["quiet_start"] if quiet_start is None else quiet_start,
        "quiet_end": current["quiet_end"] if quiet_end is None else quiet_end,
    }
    if not 0 <= merged["max_per_day"] <= 10:
        raise ValueError("每日主动消息上限只能在 0 到 10 之间")
    for key in ("quiet_start", "quiet_end"):
        if not 0 <= merged[key] <= 23:
            raise ValueError("免打扰时段必须是 0 到 23 之间的小时")

    key = _exe_key(slug, owner)
    with _get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM proactive_configs WHERE exe_key = ?", (key,)
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE proactive_configs SET enabled = ?, max_per_day = ?,"
                " quiet_start = ?, quiet_end = ?, updated_at = ? WHERE exe_key = ?",
                (
                    1 if merged["enabled"] else 0,
                    merged["max_per_day"],
                    merged["quiet_start"],
                    merged["quiet_end"],
                    _now(),
                    key,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO proactive_configs"
                " (exe_key, enabled, max_per_day, quiet_start, quiet_end)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    1 if merged["enabled"] else 0,
                    merged["max_per_day"],
                    merged["quiet_start"],
                    merged["quiet_end"],
                ),
            )
        conn.commit()
    return merged


# ── 健康联动 ──


def health_blocks(user_id: int) -> Optional[str]:
    """健康保护是否禁止推送。返回原因，或 None 表示放行。

    🔴 这个产品的用户群本就有沉溺风险，主动召回是加剧沉溺最直接的手段。
    保护优先于任何主动性。
    """
    from server.usage_guard import status

    try:
        usage = status(user_id)
    except Exception as e:  # noqa: BLE001 — 查不到状态时保守起见不推
        logger.warning("查询使用状态失败，保守跳过主动消息 user=%s: %s", user_id, e)
        return "无法确认使用状态"

    if usage.get("in_cooldown"):
        return "用户处于强制冷静期"
    if usage.get("active_seconds", 0) >= usage.get("daily_limit_seconds", 0) > 0:
        return "用户已达当日使用时长上限"

    from server.safety_store import has_recent_crisis

    if has_recent_crisis(user_id):
        return "用户近期有危机事件"
    return None


# ── 触发判定 ──


def _quiet_now(config: dict, now: datetime) -> bool:
    start, end = config["quiet_start"], config["quiet_end"]
    hour = now.hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # 跨午夜


def _sent_today(user_id: int, slug: str, now: datetime) -> int:
    from server.auth import _get_conn

    today = now.strftime("%Y-%m-%d")
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM proactive_messages"
            " WHERE user_id = ? AND slug = ? AND created_at LIKE ?",
            (user_id, slug, f"{today}%"),
        ).fetchone()
    return int(row["n"])


def decide_trigger(
    slug: str,
    user_id: int,
    *,
    owner: Optional[int] = None,
    last_seen_iso: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """判断此刻是否该主动发消息，返回触发类型或 None。"""
    now = now or datetime.now()
    config = get_config(slug, owner)
    if not config["enabled"]:
        return None
    if _quiet_now(config, now):
        return None
    if _sent_today(user_id, slug, now) >= config["max_per_day"]:
        return None

    blocked = health_blocks(user_id)
    if blocked:
        logger.info("跳过主动消息 user=%s slug=%s 原因=%s", user_id, slug, blocked)
        return None

    # 沉默最久的信号优先：好几天没聊比「早安」更值得说
    if last_seen_iso:
        try:
            last = datetime.fromisoformat(last_seen_iso)
            if last.tzinfo is not None:
                last = last.replace(tzinfo=None)
            if now - last > timedelta(hours=SILENCE_THRESHOLD_HOURS):
                return TRIGGER_SILENCE
        except (ValueError, TypeError):
            pass

    if 8 <= now.hour < 10:
        return TRIGGER_MORNING
    if 21 <= now.hour < 23:
        return TRIGGER_NIGHT
    return None


TRIGGER_PROMPTS = {
    TRIGGER_MORNING: "现在是早上，你想主动跟 ta 说句话。",
    TRIGGER_NIGHT: "现在是晚上快睡的时候，你想主动跟 ta 说句话。",
    TRIGGER_SILENCE: "你们已经好几天没说话了，你想主动找 ta。",
    TRIGGER_ANNIVERSARY: "今天对你们来说是个有意义的日子，你想说点什么。",
}


def compose(engine, trigger: str) -> str:
    """让镜像自己生成主动消息。

    走同一个引擎，所以人格、风格、时间感知全都一致——另写一套模板会让
    主动消息一眼看出是系统发的。
    """
    instruction = TRIGGER_PROMPTS.get(trigger, TRIGGER_PROMPTS[TRIGGER_MORNING])
    reply, _stickers, _usage = engine.chat(
        f"[系统提示：{instruction}只说一两句，像平时那样自然，不要解释你在做什么]",
        [],
    )
    return reply.strip()


# ── 存取 ──


def queue_message(
    slug: str, user_id: int, trigger: str, content: str, owner: Optional[int] = None
) -> Optional[int]:
    if not content.strip():
        return None
    from server.auth import _get_conn

    with _get_conn() as conn:
        message_id = conn.insert_returning_id(
            "INSERT INTO proactive_messages (exe_key, user_id, slug, trigger, content)"
            " VALUES (?, ?, ?, ?, ?)",
            (_exe_key(slug, owner), user_id, slug, trigger, content.strip()),
        )
        conn.commit()
    logger.info("已排队主动消息 user=%s slug=%s trigger=%s", user_id, slug, trigger)
    return int(message_id)


def pending_messages(user_id: int, slug: Optional[str] = None) -> list[dict]:
    from server.auth import _get_conn

    sql = (
        "SELECT id, slug, trigger, content, created_at FROM proactive_messages"
        " WHERE user_id = ? AND status = 'pending'"
    )
    params: list = [user_id]
    if slug:
        sql += " AND slug = ?"
        params.append(slug)
    sql += " ORDER BY created_at"
    with _get_conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def mark_delivered(message_ids: list[int], user_id: int) -> int:
    """标记已送达。带 user_id 校验，避免改到别人的消息。"""
    if not message_ids:
        return 0
    from server.auth import _get_conn

    updated = 0
    with _get_conn() as conn:
        for message_id in message_ids:
            cursor = conn.execute(
                "UPDATE proactive_messages SET status = 'delivered', delivered_at = ?"
                " WHERE id = ? AND user_id = ? AND status = 'pending'",
                (_now(), message_id, user_id),
            )
            updated += cursor.rowcount
        conn.commit()
    return updated
