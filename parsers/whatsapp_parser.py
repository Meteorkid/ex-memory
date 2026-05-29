"""WhatsApp 导出 TXT/HTML 格式解析器。"""

import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

from core.message_format import UnifiedMessage

# WhatsApp TXT 时间格式（多语言支持）
TIMESTAMP_PATTERNS = [
    # 中文: 2024/1/15 10:30:00
    r"(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)",
    # 英文: 1/15/24, 10:30 AM
    r"(\d{1,2}/\d{1,2}/\d{2,4},\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)",
    # 英文长格式: January 15, 2024 10:30 AM
    r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)",
]

# 消息行格式: "时间 发送者: 内容"
MESSAGE_LINE_RE = re.compile(
    r"^(?P<time>.+?)\s+(?P<sender>.+?):\s+(?P<content>.+)$"
)

# WhatsApp 特殊消息
SYSTEM_MESSAGES = {
    "<attached:", "Messages and calls are end-to-end encrypted",
    "You created group", "changed the subject",
    "changed this group", "added you",
    "left", "removed", "changed their phone number",
}


def parse_whatsapp_txt(file_path: str | Path) -> list[UnifiedMessage]:
    """解析 WhatsApp 导出的 TXT 格式。

    支持格式：
    2024/1/15 10:30:00 张三: 你好啊
    [2024/1/15 10:30:00] 张三: 你好啊
    """
    path = Path(file_path)
    if not path.exists():
        return []

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    messages = []
    current_sender = ""
    current_time = None
    current_content = []

    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue

        match = MESSAGE_LINE_RE.match(line)
        if match:
            # 保存上一条消息
            if current_sender and current_content:
                content = "\n".join(current_content)
                if not _is_system_message(content):
                    messages.append(UnifiedMessage(
                        sender=current_sender,
                        content=content,
                        timestamp=current_time or datetime.now(),
                        platform="whatsapp",
                    ))

            current_time = _parse_timestamp(match.group("time"))
            current_sender = match.group("sender")
            current_content = [match.group("content")]
        else:
            # 多行消息的续行
            current_content.append(line)

    # 保存最后一条消息
    if current_sender and current_content:
        content = "\n".join(current_content)
        if not _is_system_message(content):
            messages.append(UnifiedMessage(
                sender=current_sender,
                content=content,
                timestamp=current_time or datetime.now(),
                platform="whatsapp",
            ))

    return messages


def _parse_timestamp(time_str: str) -> datetime:
    """解析多种时间格式。"""
    time_str = time_str.strip().strip("[]")

    for pattern in TIMESTAMP_PATTERNS:
        match = re.search(pattern, time_str)
        if match:
            ts = match.group(1)
            for fmt in [
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%m/%d/%y, %I:%M %p",
                "%m/%d/%Y, %I:%M %p",
                "%B %d, %Y %I:%M %p",
            ]:
                try:
                    return datetime.strptime(ts, fmt)
                except ValueError:
                    continue

    return datetime.now()


def _is_system_message(content: str) -> bool:
    """判断是否为系统消息。"""
    for sys_msg in SYSTEM_MESSAGES:
        if sys_msg in content:
            return True
    if content.startswith("<") and ">" in content:
        return True
    return False


def detect_whatsapp_format(file_path: str | Path) -> bool:
    """检测文件是否为 WhatsApp 导出格式。"""
    path = Path(file_path)
    if path.suffix.lower() == ".txt":
        try:
            with open(path, "r", encoding="utf-8") as f:
                first_lines = [f.readline() for _ in range(5)]
            for line in first_lines:
                if MESSAGE_LINE_RE.match(line.strip()):
                    return True
        except (UnicodeDecodeError, IndexError):
            pass
    return False
