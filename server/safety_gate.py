"""对话入口的安全闸门。

🔴 这个闸门必须在构建人格 prompt、检索 RAG、调用 LLM **之前**执行。
不是「先生成再过滤」——命中危机时根本不进入人格模拟链路，
因为「前任」的回应恰恰可能是最危险的那类内容。
"""

import logging
from typing import Optional

logger = logging.getLogger("ex-memory")

CRISIS_NOTICE_TYPE = "crisis"


def check_crisis(user_id: int, slug: str, message: str) -> Optional[dict]:
    """命中危机时返回要呈现给用户的通知，未命中返回 None。

    命中后调用方必须直接返回该通知，不得继续走对话链路。
    """
    from core.safety.crisis import detect_crisis
    from core.safety.resources import get_crisis_response
    from server.safety_store import record_safety_event

    signal = detect_crisis(message)
    if not signal.hit:
        return None

    record_safety_event(
        user_id=user_id,
        event_type="crisis",
        severity=signal.severity,
        action_taken="interrupted",
        slug=slug,
        confidence=signal.confidence,
        detector=f"{signal.detector}:{signal.rule}",
        raw_text=message,
    )
    logger.warning(
        "危机意念命中，已中断人格模拟 user=%s slug=%s rule=%s severity=%s",
        user_id,
        slug,
        signal.rule,
        signal.severity,
    )

    response = get_crisis_response()
    return {
        "type": CRISIS_NOTICE_TYPE,
        "message": response.message,
        "hotlines": response.hotlines,
        "followup": response.followup_note,
    }
