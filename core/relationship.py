"""关系时间线与跨会话状态（FR-065 / FR-066）。

现有三层记忆都是**空间维度**的：ta 说过什么、ta 是什么样的人。缺的是
时间维度与连续性：

- **时间线**：结构化的共同经历。「去年这时候我们还在…」需要能直接查，
  而不是从一堆会话摘要里现找。
- **跨会话状态**：ta 今天心情如何、最近在忙什么。没有它，每次新会话 ta
  都从同一个初始状态开始——那不像个活着的人，像个每次重启的程序。
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("ex-memory")

MAX_TIMELINE_IN_PROMPT = 8


def exe_key(slug: str, owner: Optional[int] = None) -> str:
    """与向量库 collection 一致的镜像标识。"""
    return f"{owner}/{slug}" if owner is not None else slug


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 时间线 ──


def add_event(
    slug: str,
    event: str,
    *,
    owner: Optional[int] = None,
    happened_at: Optional[str] = None,
    emotion: Optional[str] = None,
    source: str = "session",
    source_ref: Optional[str] = None,
) -> int:
    from server.auth import _get_conn

    if not event.strip():
        raise ValueError("事件描述不能为空")
    with _get_conn() as conn:
        event_id = conn.insert_returning_id(
            """
            INSERT INTO relationship_timeline
                (exe_key, happened_at, event, emotion, source, source_ref)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                exe_key(slug, owner),
                happened_at,
                event.strip(),
                emotion,
                source,
                source_ref,
            ),
        )
        conn.commit()
        return int(event_id)


def list_events(slug: str, owner: Optional[int] = None, limit: int = 50) -> list[dict]:
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, happened_at, event, emotion, source, created_at
            FROM relationship_timeline
            WHERE exe_key = ?
            ORDER BY COALESCE(happened_at, created_at)
            LIMIT ?
            """,
            (exe_key(slug, owner), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_event(event_id: int, slug: str, owner: Optional[int] = None) -> bool:
    """删除时间线条目。带 exe_key 校验，避免删到别的镜像。"""
    from server.auth import _get_conn

    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM relationship_timeline WHERE id = ? AND exe_key = ?",
            (event_id, exe_key(slug, owner)),
        )
        conn.commit()
        return cursor.rowcount > 0


def timeline_prompt(slug: str, owner: Optional[int] = None) -> str:
    """时间线的 prompt 片段。空时返回空串，不占 token。"""
    events = list_events(slug, owner, limit=MAX_TIMELINE_IN_PROMPT)
    if not events:
        return ""
    lines = ["\n---\n## 你们的共同经历（按时间）"]
    for e in events:
        when = e.get("happened_at") or ""
        emotion = f"（{e['emotion']}）" if e.get("emotion") else ""
        lines.append(f"- {when} {e['event']}{emotion}".strip())
    lines.append("提到相关话题时可以自然带出这些事，但不要刻意罗列。\n")
    return "\n".join(lines)


# ── 跨会话状态 ──


def get_state(slug: str, owner: Optional[int] = None) -> Optional[dict]:
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT mood, recent_context, updated_at FROM exe_states WHERE exe_key = ?",
            (exe_key(slug, owner),),
        ).fetchone()
    return dict(row) if row else None


def set_state(
    slug: str,
    *,
    owner: Optional[int] = None,
    mood: Optional[str] = None,
    recent_context: Optional[str] = None,
) -> None:
    """更新状态。只覆盖传入的字段，避免一次更新把另一半抹掉。"""
    from server.auth import _get_conn

    key = exe_key(slug, owner)
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT mood, recent_context FROM exe_states WHERE exe_key = ?", (key,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO exe_states (exe_key, mood, recent_context, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (key, mood, recent_context, _now()),
            )
        else:
            conn.execute(
                "UPDATE exe_states SET mood = ?, recent_context = ?, updated_at = ?"
                " WHERE exe_key = ?",
                (
                    mood if mood is not None else existing["mood"],
                    recent_context
                    if recent_context is not None
                    else existing["recent_context"],
                    _now(),
                    key,
                ),
            )
        conn.commit()


def state_prompt(slug: str, owner: Optional[int] = None) -> str:
    """当前状态的 prompt 片段。"""
    state = get_state(slug, owner)
    if not state or not (state.get("mood") or state.get("recent_context")):
        return ""
    lines = ["\n---\n## 你现在的状态"]
    if state.get("mood"):
        lines.append(f"心情：{state['mood']}")
    if state.get("recent_context"):
        lines.append(f"最近：{state['recent_context']}")
    lines.append("这些会自然影响你的语气，但不必主动汇报。\n")
    return "\n".join(lines)


def clear_for_mirror(slug: str, owner: Optional[int] = None) -> None:
    """镜像删除时清理。时间线与状态在库里，不随目录一起消失。"""
    from server.auth import _get_conn

    key = exe_key(slug, owner)
    with _get_conn() as conn:
        conn.execute("DELETE FROM relationship_timeline WHERE exe_key = ?", (key,))
        conn.execute("DELETE FROM exe_states WHERE exe_key = ?", (key,))
        conn.commit()
