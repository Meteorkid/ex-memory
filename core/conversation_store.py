"""服务端对话归档：Web/API 聊天 JSONL 持久化。"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Optional

from core.privacy import mask_sensitive

_CONVERSATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def validate_conversation_id(conversation_id: str) -> str:
    if not isinstance(conversation_id, str) or not _CONVERSATION_ID.fullmatch(
        conversation_id
    ):
        raise ValueError("会话标识无效")
    return conversation_id


def conversation_key(conversation_id: str) -> str:
    value = validate_conversation_id(conversation_id)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _store(slug: str, owner: Optional[int] = None):
    from core.mirror_store import mirror_store

    return mirror_store(slug, owner)


def append_turn(
    slug: str,
    user_id: int,
    user_message: str,
    assistant_reply: str,
    stickers: Optional[list[str]] = None,
    source: str = "web",
    conversation_id: str = "web",
    sources: Optional[list[dict]] = None,
    conversation_label: Optional[str] = None,
) -> str:
    """追加一轮对话到 `conversations/conversation.jsonl`。

    手机号/身份证/银行卡/邮箱在落库前脱敏——这四类对语气还原没有价值，
    敏感信息不以明文入库。
    """
    conversation_id = validate_conversation_id(conversation_id)
    if sources is not None and not isinstance(sources, list):
        raise ValueError("回答出处必须是列表")
    saved_sources = []
    for source_item in (sources or [])[:20]:
        if not isinstance(source_item, dict):
            raise ValueError("回答出处格式无效")
        item = json.loads(json.dumps(source_item, ensure_ascii=False))
        for field in ("text", "content", "excerpt", "display_text"):
            if isinstance(item.get(field), str):
                item[field] = mask_sensitive(item[field])
        saved_sources.append(item)
    turn_id = uuid.uuid4().hex
    created_at = datetime.now().isoformat()
    records = [
        {
            "id": f"{turn_id}-user",
            "turn_id": turn_id,
            "role": "user",
            "content": mask_sensitive(user_message),
            "created_at": created_at,
            "source": source,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "conversation_label": (conversation_label or "")[:120],
        },
        {
            "id": f"{turn_id}-assistant",
            "turn_id": turn_id,
            "role": "assistant",
            "content": mask_sensitive(assistant_reply),
            "created_at": datetime.now().isoformat(),
            "source": source,
            "user_id": user_id,
            "stickers": stickers or [],
            "conversation_id": conversation_id,
            "conversation_label": (conversation_label or "")[:120],
            "sources": saved_sources,
        },
    ]
    _store(slug, user_id).append_jsonl("conversations/conversation.jsonl", records)
    return turn_id


def load_jsonl_messages(
    slug: str, owner: Optional[int] = None, conversation_id: Optional[str] = None
) -> list[dict]:
    """读取 Web/API 对话归档。损坏行会被跳过。"""
    store = _store(slug, owner)
    if conversation_id is not None:
        validate_conversation_id(conversation_id)
    messages = []
    for rel in store.list("conversations", "*.jsonl"):
        path = store.path(rel)
        from core.file_utils import _lock

        # 与 append_jsonl / 留存清理共用 sidecar 锁，不读取半轮对话。
        with open(path.with_name(path.name + ".lock"), "a+", encoding="utf-8") as lock:
            _lock(lock, fcntl.LOCK_SH)
            lines = store.read_text(rel).splitlines()
        occurrences: dict[str, int] = {}
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if (
                item.get("role") not in ("user", "assistant")
                or not isinstance(item.get("content"), str)
                or not item["content"]
            ):
                continue
            if conversation_id is not None:
                if item.get("conversation_id", "web") != conversation_id:
                    continue
                item = dict(item)
                item["conversation_id"] = conversation_id
                if not isinstance(item.get("id"), str) or not item["id"]:
                    canonical = json.dumps(
                        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    duplicate_index = occurrences.get(canonical, 0)
                    occurrences[canonical] = duplicate_index + 1
                    # 留存清理会移动行号或重新序列化 JSON；内容不变的引用仍须稳定。
                    material = f"{rel}:{canonical}:{duplicate_index}"
                    item["id"] = (
                        "legacy_" + hashlib.sha256(material.encode()).hexdigest()[:24]
                    )
            messages.append(item)
    return messages


def load_history(
    slug: str,
    owner: Optional[int],
    conversation_id: str = "web",
    limit: int = 40,
    max_characters: int = 12000,
) -> list[dict]:
    if (
        type(limit) is not int
        or limit < 1
        or type(max_characters) is not int
        or max_characters < 1
    ):
        raise ValueError("会话历史上限无效")
    messages = load_jsonl_messages(slug, owner, conversation_id)[-min(limit, 200) :]
    history = [{"role": item["role"], "content": item["content"]} for item in messages]
    while history and sum(len(item["content"]) for item in history) > max_characters:
        history.pop(0)
    return history


def list_conversations(slug: str, owner: Optional[int]) -> list[dict]:
    conversations: dict[str, dict] = {}
    for message in load_jsonl_messages(slug, owner):
        conversation_id = message.get("conversation_id", "web")
        try:
            validate_conversation_id(conversation_id)
        except ValueError:
            continue
        entry = conversations.setdefault(
            conversation_id,
            {
                "conversation_id": conversation_id,
                "label": "网页会话" if conversation_id == "web" else conversation_id,
                "source": message.get("source", "web"),
                "updated_at": "",
                "message_count": 0,
            },
        )
        if (
            isinstance(message.get("conversation_label"), str)
            and message["conversation_label"]
        ):
            entry["label"] = message["conversation_label"][:120]
        if isinstance(message.get("created_at"), str):
            entry["updated_at"] = max(entry["updated_at"], message["created_at"])
        entry["message_count"] += 1
    return sorted(
        conversations.values(), key=lambda item: item["updated_at"], reverse=True
    )


def get_message(
    slug: str, owner: Optional[int], message_id: str, conversation_id: str = "web"
) -> Optional[dict]:
    if not isinstance(message_id, str) or not message_id:
        return None
    return next(
        (
            item
            for item in load_jsonl_messages(slug, owner, conversation_id)
            if item["id"] == message_id
        ),
        None,
    )


@contextmanager
def conversation_request_lock(slug: str, owner: Optional[int], conversation_id: str):
    store = _store(slug, owner)
    path = store.path(
        f"conversations/.request-{conversation_key(conversation_id)}.lock"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TimeoutError("该会话正在回复，请稍后重试") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
