"""统一消息格式：跨平台消息标准化。"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class UnifiedMessage:
    """统一消息格式，用于跨平台解析器。"""
    sender: str
    content: str
    timestamp: datetime
    msg_type: str = "text"  # text/image/voice/video/file/sticker
    platform: str = "unknown"  # wechat/qq/telegram/whatsapp/imessage
    media_path: Optional[str] = None
    reply_to: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "sender": self.sender,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "msg_type": self.msg_type,
            "platform": self.platform,
            "media_path": self.media_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UnifiedMessage":
        ts = data.get("timestamp", "")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                ts = datetime.now()
        return cls(
            sender=data.get("sender", ""),
            content=data.get("content", ""),
            timestamp=ts,
            msg_type=data.get("msg_type", "text"),
            platform=data.get("platform", "unknown"),
            media_path=data.get("media_path"),
        )
