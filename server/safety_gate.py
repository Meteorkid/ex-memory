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


def check_input(user_id: int, slug: str, message: str) -> Optional[dict]:
    """输入内容审核。被拦截时返回通知，放行返回 None。

    🔴 必须在 check_crisis 之后调用：一条消息既命中危机又命中违规词时，
    不能因为「违规」把正在求救的人挡回去。
    """
    from core.safety.moderation import moderate_input
    from server.safety_store import record_safety_event

    result = moderate_input(message)
    if result.allowed:
        return None

    record_safety_event(
        user_id=user_id,
        event_type="content_input",
        severity=result.severity,
        action_taken="blocked",
        slug=slug,
        detector=f"{result.detector}:{result.category}",
        raw_text=message,
    )
    logger.warning(
        "输入内容被拦截 user=%s slug=%s category=%s detector=%s",
        user_id,
        slug,
        result.category,
        result.detector,
    )
    return {
        "type": "blocked",
        "message": "这条消息包含无法处理的内容，换个说法再试试吧。",
    }


def check_output_streaming(user_id: int, slug: str, text: str) -> Optional[dict]:
    """流式热路径的输出审核：仅本地词表，逐块对累计全文调用。

    保证来自「下发前检查累计全文」——违规词一旦完整出现就被拦下，
    承载它的那个分块不会下发。
    """
    from core.safety.moderation import moderate_output_local
    from server.safety_store import record_safety_event

    result = moderate_output_local(text)
    if result.allowed:
        return None
    record_safety_event(
        user_id=user_id,
        event_type="content_output",
        severity=result.severity,
        action_taken="blocked",
        slug=slug,
        detector=f"{result.detector}:{result.category}",
        raw_text=text,
    )
    logger.warning(
        "流式输出被本地词表拦截 user=%s slug=%s category=%s",
        user_id,
        slug,
        result.category,
    )
    return {
        "type": "blocked",
        "message": "这次没能好好回应你，我们再聊点别的吧。",
    }


def check_output(user_id: int, slug: str, text: str) -> Optional[dict]:
    """输出内容审核。被拦截时返回通知，放行返回 None。

    调用方必须保证被拦截的内容既不下发也不落库。
    """
    from core.safety.moderation import moderate_output
    from server.safety_store import record_safety_event

    result = moderate_output(text)
    if result.allowed:
        return None

    record_safety_event(
        user_id=user_id,
        event_type="content_output",
        severity=result.severity,
        action_taken="blocked",
        slug=slug,
        detector=f"{result.detector}:{result.category}",
        raw_text=text,
    )
    logger.warning(
        "模型输出被拦截 user=%s slug=%s category=%s", user_id, slug, result.category
    )
    return {
        "type": "blocked",
        "message": "这次没能好好回应你，我们再聊点别的吧。",
    }
