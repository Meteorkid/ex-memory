"""服务端对话归档：Web/API 聊天 JSONL 持久化。"""

import fcntl
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import config

LOCK_TIMEOUT = 5


def append_turn(
    slug: str,
    user_id: int,
    user_message: str,
    assistant_reply: str,
    stickers: Optional[list[str]] = None,
    source: str = "web",
) -> None:
    """追加一轮对话到 `conversations/conversation.jsonl`。"""
    path = _conversation_path(slug)
    turn_id = uuid.uuid4().hex
    created_at = datetime.now().isoformat()
    records = [
        {
            "id": f"{turn_id}-user",
            "turn_id": turn_id,
            "role": "user",
            "content": user_message,
            "created_at": created_at,
            "source": source,
            "user_id": user_id,
        },
        {
            "id": f"{turn_id}-assistant",
            "turn_id": turn_id,
            "role": "assistant",
            "content": assistant_reply,
            "created_at": datetime.now().isoformat(),
            "source": source,
            "user_id": user_id,
            "stickers": stickers or [],
        },
    ]
    _append_jsonl(path, records)


def load_jsonl_messages(slug: str) -> list[dict]:
    """读取 Web/API 对话归档。损坏行会被跳过。"""
    directory = config.get_ex_dir(slug) / "conversations"
    if not directory.exists():
        return []

    messages = []
    for path in sorted(directory.glob("*.jsonl")):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
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


def _conversation_path(slug: str) -> Path:
    path = config.get_ex_dir(slug) / "conversations" / "conversation.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_jsonl(path: Path, records: list[dict]) -> None:
    lock_path = path.with_name(path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        _lock(lock_file, fcntl.LOCK_EX)
        with open(path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()


def _lock(f, mode: int):
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"无法获取文件锁: {f.name}")
            time.sleep(0.05)
