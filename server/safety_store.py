"""安全事件落库。

存的是**脱敏并截断**的片段，不是原话全文的等价物——但要说清楚边界：
危机消息通常很短，短于截断长度时片段就等于整条（已脱敏）消息。这是有意
的取舍：复核者必须看得懂内容才能分辨真实求救与误报，全哈希会让复核队列
失去意义。

因此这张表的真正防线不是「片段不可读」，而是：
1. 手机号/身份证/银行卡/邮箱等 PII 一律脱敏后再入库；
2. 长度封顶，不留长篇上下文；
3. 原话全文绝不进应用日志；
4. 复核队列的访问必须受权限控制（尚未实现，见 PRD M0 后续）。
"""

import hashlib
import logging
from typing import Optional

logger = logging.getLogger("ex-memory")

# 复核者需要一点上下文才能分辨真实求救与误报，全哈希会让复核队列失去意义；
# 但片段越长泄露面越大，取一个够判断、不够还原的长度。
EXCERPT_MAX_CHARS = 30


def _excerpt(text: str) -> str:
    from core.privacy import mask_sensitive

    return mask_sensitive(text)[:EXCERPT_MAX_CHARS]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_safety_event(
    user_id: int,
    event_type: str,
    severity: str,
    action_taken: str,
    *,
    slug: Optional[str] = None,
    confidence: Optional[float] = None,
    detector: Optional[str] = None,
    raw_text: Optional[str] = None,
) -> Optional[int]:
    """记录一次安全事件，返回事件 ID。

    落库失败不抛异常：这条路径挂在对话主链路上，审计写失败不能连带
    把危机响应也弄没了。但必须留日志，否则审计缺口无人察觉。
    """
    from server.auth import _get_conn

    try:
        with _get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO safety_events (
                    user_id, slug, event_type, severity, confidence,
                    detector, input_hash, excerpt, action_taken
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    slug,
                    event_type,
                    severity,
                    confidence,
                    detector,
                    _text_hash(raw_text) if raw_text else None,
                    _excerpt(raw_text) if raw_text else None,
                    action_taken,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
    except Exception as e:  # noqa: BLE001 — 审计失败不得中断危机响应
        logger.error("安全事件落库失败 type=%s user=%s: %s", event_type, user_id, e)
        return None


def list_pending_reviews(limit: int = 50) -> list[dict]:
    """人工复核队列：按严重度与时间取待复核事件。"""
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, slug, event_type, severity, confidence,
                   detector, excerpt, action_taken, created_at
            FROM safety_events
            WHERE review_status = 'pending'
            ORDER BY CASE severity WHEN 'high' THEN 0 ELSE 1 END, created_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def resolve_review(
    event_id: int, reviewed_by: str, status: str, note: str = ""
) -> bool:
    """标记复核结果。status: resolved / dismissed。"""
    if status not in ("resolved", "dismissed"):
        raise ValueError("复核状态只能是 resolved 或 dismissed")
    from server.auth import _get_conn

    with _get_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE safety_events
            SET review_status = ?, reviewed_by = ?, review_note = ?,
                reviewed_at = datetime('now')
            WHERE id = ?
            """,
            (status, reviewed_by, note, event_id),
        )
        conn.commit()
        return cursor.rowcount > 0
