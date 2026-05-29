"""iMessage 导出 CSV 格式解析器。"""

import csv
from datetime import datetime
from pathlib import Path

from core.message_format import UnifiedMessage


def parse_imessage_csv(file_path: str | Path) -> list[UnifiedMessage]:
    """解析 iMessage 导出的 CSV 格式。

    支持 imessage-exporter 工具的 CSV 输出格式：
    序列, 发送者, 时间, 类型, 内容, ...
    """
    path = Path(file_path)
    if not path.exists():
        return []

    messages = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sender = row.get("Sender", row.get("sender", row.get("From", "")))
            content = row.get("Text", row.get("text", row.get("Content", "")))
            time_str = row.get("Date", row.get("date", row.get("Time", "")))
            msg_type = row.get("Type", row.get("type", "Text"))

            if not content:
                continue

            timestamp = _parse_timestamp(time_str)
            platform_type = _determine_type(msg_type)

            messages.append(UnifiedMessage(
                sender=sender,
                content=content.strip(),
                timestamp=timestamp,
                msg_type=platform_type,
                platform="imessage",
            ))

    return messages


def _parse_timestamp(time_str: str) -> datetime:
    """解析 iMessage 时间戳。"""
    if not time_str:
        return datetime.now()

    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ]:
        try:
            return datetime.strptime(time_str.strip(), fmt)
        except ValueError:
            continue

    return datetime.now()


def _determine_type(type_str: str) -> str:
    """判断消息类型。"""
    type_lower = type_str.lower() if type_str else ""
    if "image" in type_lower or "photo" in type_lower:
        return "image"
    if "video" in type_lower:
        return "video"
    if "audio" in type_lower:
        return "voice"
    if "attachment" in type_lower:
        return "file"
    return "text"


def detect_imessage_format(file_path: str | Path) -> bool:
    """检测文件是否为 iMessage CSV 导出格式。"""
    path = Path(file_path)
    if path.suffix.lower() != ".csv":
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return False
            header_lower = [h.lower().strip() for h in header]
            # iMessage CSV 通常包含 sender/date/text 等列
            has_sender = any(h in header_lower for h in ["sender", "from", "发送者"])
            has_text = any(h in header_lower for h in ["text", "content", "内容", "message"])
            return has_sender and has_text
    except (csv.Error, UnicodeDecodeError, StopIteration):
        return False
