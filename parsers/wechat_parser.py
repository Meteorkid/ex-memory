"""微信聊天记录解析器。

支持格式：
- WeFlow 导出 (JSON / JSONL)
- WeChatMsg 导出 (txt)
- 留痕导出 (JSON)
- 纯文本粘贴
"""

import json
import re
from pathlib import Path


def detect_format(file_path: str) -> str:
    """自动检测文件格式。"""
    p = Path(file_path)
    ext = p.suffix.lower()

    if ext == ".jsonl":
        return "weflow_jsonl"
    if ext == ".json":
        # 区分 WeFlow 和留痕
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(2000)
        if '"formattedTime"' in head or '"isSend"' in head:
            return "liuhen"
        if '"messages"' in head:
            return "weflow_json"
        return "weflow_json"
    if ext == ".txt":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(2000)
        if re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", head):
            return "wechatmsg_txt"
        return "plaintext"

    return "plaintext"


def parse_weflow_json(file_path: str, target_name: str = "") -> list[dict]:
    """解析 WeFlow 导出的 JSON。"""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages_raw = data if isinstance(data, list) else data.get("messages", [])
    return _normalize_messages(messages_raw, target_name)


def parse_weflow_jsonl(file_path: str, target_name: str = "") -> list[dict]:
    """解析 WeFlow 导出的 JSONL。"""
    messages_raw = []
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    messages_raw.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return _normalize_messages(messages_raw, target_name)


def parse_liuhen(file_path: str, target_name: str = "") -> list[dict]:
    """解析留痕导出的 JSON。"""
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    messages_raw = data if isinstance(data, list) else data.get("messages", [])
    return _normalize_messages(messages_raw, target_name)


def parse_wechatmsg_txt(file_path: str, target_name: str = "") -> list[dict]:
    """解析 WeChatMsg 导出的 txt。格式: 2024-01-15 20:30:45 张三\\n内容"""
    messages = []
    current_msg = None
    msg_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(.+)$")

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")
            match = msg_pattern.match(line)
            if match:
                if current_msg:
                    messages.append(current_msg)
                timestamp, sender = match.groups()
                current_msg = {"timestamp": timestamp, "sender": sender.strip(), "content": ""}
            elif current_msg and line.strip():
                if current_msg["content"]:
                    current_msg["content"] += "\n"
                current_msg["content"] += line

    if current_msg:
        messages.append(current_msg)

    return _filter_text_messages(messages)


def parse_plaintext(file_path: str, target_name: str = "") -> list[dict]:
    """解析纯文本。"""
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    return [{
        "timestamp": "",
        "sender": target_name or "unknown",
        "content": content,
        "is_target": True,
    }]


def _normalize_messages(messages_raw: list, target_name: str) -> list[dict]:
    """统一消息格式。适配 WeFlow 和留痕的字段差异。"""
    normalized = []
    for msg in messages_raw:
        # WeFlow 格式
        if "sender" in msg and "content" in msg:
            sender = msg.get("sender", "")
            # WeFlow 的 isSend: 1=我发的, 0=ta发的
            is_send = msg.get("isSend", 0)
            if isinstance(is_send, str):
                is_send = int(is_send)
            is_me = bool(is_send)
        # 留痕格式
        elif "senderDisplayName" in msg:
            sender = msg.get("senderDisplayName", "")
            is_me = msg.get("isSend") == 1
        else:
            sender = msg.get("sender", "unknown")
            is_me = False

        content = msg.get("content", "")
        # 过滤非文本消息
        if not content or content in ("[图片]", "[语音]", "[视频]", "[文件]", "[表情]"):
            continue

        normalized.append({
            "timestamp": msg.get("formattedTime", msg.get("timestamp", "")),
            "sender": "我" if is_me else (target_name or sender),
            "content": content,
            "is_target": not is_me,
        })

    return _filter_text_messages(normalized)


def _filter_text_messages(messages: list[dict]) -> list[dict]:
    """过滤只保留文本消息。"""
    return [m for m in messages if m.get("content", "").strip()]


def parse(file_path: str, target_name: str = "", fmt: str = "auto") -> list[dict]:
    """统一入口：自动检测格式并解析。

    Returns:
        标准化消息列表，每条含 timestamp, sender, content, is_target
    """
    if fmt == "auto":
        fmt = detect_format(file_path)

    parsers = {
        "weflow_json": parse_weflow_json,
        "weflow_jsonl": parse_weflow_jsonl,
        "liuhen": parse_liuhen,
        "wechatmsg_txt": parse_wechatmsg_txt,
        "plaintext": parse_plaintext,
    }

    parser_func = parsers.get(fmt, parse_plaintext)
    return parser_func(file_path, target_name)
