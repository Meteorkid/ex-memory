"""Telegram Desktop 导出 JSON 格式解析器。"""

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator

from core.message_format import UnifiedMessage


def parse_telegram_json(file_path: str | Path) -> list[UnifiedMessage]:
    """解析 Telegram Desktop 导出的 result.json 格式。

    Telegram Desktop 导出格式：
    {
        "name": "Chat Title",
        "type": "personal_chat",
        "id": 123456,
        "messages": [
            {
                "id": 1,
                "type": "message",
                "date": "2024-01-15T10:30:00",
                "from": "Username",
                "text": "Hello",
                "media_type": "photo"  // 可选
            }
        ]
    }
    """
    path = Path(file_path)
    if not path.exists():
        return []

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages = []
    for msg in data.get("messages", []):
        if msg.get("type") != "message":
            continue

        sender = msg.get("from", msg.get("actor", "Unknown"))
        content = _extract_text(msg.get("text", ""))
        timestamp = _parse_timestamp(msg.get("date", ""))
        msg_type = _determine_type(msg)

        if not content and msg_type == "text":
            continue

        messages.append(UnifiedMessage(
            sender=sender,
            content=content,
            timestamp=timestamp,
            msg_type=msg_type,
            platform="telegram",
        ))

    return messages


def _extract_text(text) -> str:
    """提取文本内容，处理 Telegram 的富文本格式。"""
    if isinstance(text, str):
        return text.strip()
    if isinstance(text, list):
        parts = []
        for item in text:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text", ""))
        return "".join(parts).strip()
    return ""


def _parse_timestamp(date_str: str) -> datetime:
    """解析 Telegram 时间戳格式。"""
    try:
        return datetime.fromisoformat(date_str)
    except (ValueError, TypeError):
        return datetime.now()


def _determine_type(msg: dict) -> str:
    """根据消息内容判断类型。"""
    media = msg.get("media_type", "")
    if media in ("photo", "sticker"):
        return "image"
    if media == "voice_message":
        return "voice"
    if media == "video_message":
        return "video"
    if media in ("document", "animation"):
        return "file"
    return "text"


def detect_telegram_format(file_path: str | Path) -> bool:
    """检测文件是否为 Telegram 导出格式。"""
    path = Path(file_path)
    if not path.suffix == ".json":
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return "messages" in data and ("name" in data or "type" in data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
