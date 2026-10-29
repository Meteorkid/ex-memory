"""Web 与 MCP 共用的会话读取、回答参考和显式纠正。"""

from typing import Optional

from core.exe_access import assert_exe_access
from core.validation import sanitize_chat_history


def request_history(slug, user_id, history, conversation_id: Optional[str]):
    if conversation_id is None:
        return sanitize_chat_history(history or [])
    from core.conversation_store import load_history, validate_conversation_id

    return load_history(slug, user_id, validate_conversation_id(conversation_id))


def chat_with_sources(engine, message, history, conversation_id=None):
    # 检查类上的能力，避免动态代理/旧客户端临时生成不存在的方法。
    if callable(getattr(type(engine), "chat_with_sources", None)):
        return engine.chat_with_sources(
            message, history, conversation_id=conversation_id
        )
    reply, stickers, usage = engine.chat(message, history)
    return reply, stickers, usage, []


def stream_with_sources(engine, message, history, conversation_id=None):
    if callable(getattr(type(engine), "chat_stream_with_sources", None)):
        return engine.chat_stream_with_sources(
            message, history, conversation_id=conversation_id
        )
    return engine.chat_stream(message, history)


def conversation_list(slug, user_id):
    from core.conversation_store import list_conversations

    assert_exe_access(slug, user_id)
    return {
        "user_id": user_id,
        "default_conversation_id": "web",
        "conversations": list_conversations(slug, user_id),
    }


def conversation_messages(slug, user_id, conversation_id):
    from core.conversation_store import load_jsonl_messages, validate_conversation_id
    from core.session_archive import get_archive_status

    assert_exe_access(slug, user_id)
    conversation_id = validate_conversation_id(conversation_id)
    messages = load_jsonl_messages(slug, user_id, conversation_id=conversation_id)
    fields = ("id", "role", "content", "created_at", "stickers", "sources")
    archive = get_archive_status(slug, user_id, conversation_id)
    return {
        "user_id": user_id,
        "conversation_id": conversation_id,
        "messages": [{k: m[k] for k in fields if k in m} for m in messages[-200:]],
        "archive": {**archive, "state": archive.get("status")},
    }


def save_correction(
    slug, user_id, content, conversation_id="web", message_id=None, source_ids=None
):
    from core.conversation_store import get_message, validate_conversation_id
    from core.corrections import add, load
    from core.memory_state import get_memory_revision
    from server.safety_gate import check_input

    assert_exe_access(slug, user_id)
    conversation_id = validate_conversation_id(conversation_id)
    if not isinstance(content, str) or not content.strip() or len(content) > 500:
        raise ValueError("纠正内容需为1至500字")
    source_ids = [] if source_ids is None else source_ids
    if (
        not isinstance(source_ids, list)
        or len(source_ids) > 16
        or any(not isinstance(x, str) or len(x) > 128 for x in source_ids)
    ):
        raise ValueError("来源引用格式无效")
    if source_ids and not message_id:
        raise ValueError("引用来源时必须指定已保存的回答")
    if message_id:
        message = get_message(
            slug, user_id, message_id, conversation_id=conversation_id
        )
        if message is None or message.get("role") != "assistant":
            raise ValueError("找不到当前会话中可纠正的回答")
        allowed = {s.get("id") for s in message.get("sources", [])}
        if any(source_id not in allowed for source_id in source_ids):
            raise ValueError("来源不属于这条回答")
    if check_input(user_id, slug, content) is not None:
        raise ValueError("纠正内容未通过审核")
    entry = add(
        slug,
        content.strip(),
        owner=user_id,
        context={
            "conversation_id": conversation_id,
            "message_id": message_id,
            "source_ids": list(dict.fromkeys(source_ids)),
        },
    )
    return {
        "ok": True,
        "entry": entry,
        "total": len(load(slug, user_id)),
        "revision": get_memory_revision(slug, user_id),
    }


def schedule_archive(slug, user_id, conversation_id):
    """只在达到阈值时复用已有任务队列；文件锁防止重复归档。"""
    import config
    from core.session_archive import get_archive_status, maybe_archive
    from core.tasks import enqueue, register

    status = get_archive_status(slug, user_id, conversation_id)
    if status.get("pending_turns", 0) < config.ARCHIVE_THRESHOLD:
        return None

    @register("memory_archive")
    def archive(progress, slug, owner, conversation_id):
        assert_exe_access(slug, owner)
        progress.update(10, "整理会话记忆")
        maybe_archive(slug, owner=owner, conversation_id=conversation_id)
        return get_archive_status(slug, owner, conversation_id)

    return enqueue(
        "memory_archive",
        user_id,
        {"slug": slug, "owner": user_id, "conversation_id": conversation_id},
        slug=slug,
    )
