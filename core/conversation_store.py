"""服务端对话归档：Web/API 聊天 JSONL 持久化。"""

import json
import uuid
from datetime import datetime
from typing import Optional

from core.privacy import mask_sensitive


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
) -> None:
    """追加一轮对话到 `conversations/conversation.jsonl`。

    手机号/身份证/银行卡/邮箱在落库前脱敏——这四类对语气还原没有价值，
    敏感信息不以明文入库。
    """
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
        },
    ]
    _store(slug, user_id).append_jsonl("conversations/conversation.jsonl", records)


def load_jsonl_messages(slug: str, owner: Optional[int] = None) -> list[dict]:
    """读取 Web/API 对话归档。损坏行会被跳过。"""
    store = _store(slug, owner)
    messages = []
    for rel in store.list("conversations", "*.jsonl"):
        for line in store.read_text(rel).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("role") in ("user", "assistant") and item.get("content"):
                messages.append(item)
    return messages