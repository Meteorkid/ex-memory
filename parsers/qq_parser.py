"""QQ 聊天记录解析器。

支持格式：
- QQ 导出 TXT（时间戳 + 发送者格式）
- MHT 格式（beta，基础支持）

标准输出：list[dict]，每条含 timestamp, sender, content, is_target
"""

import re
from pathlib import Path


# ── QQ TXT 格式正则 ──

# 格式 A：2024-01-01 10:00:00 发送者名(12345)
_MSG_A = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(.+?)\((\d+)\)\s*$"
)

# 格式 B：2024-01-01 10:00:00 发送者名
_MSG_B = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(.+?)\s*$"
)

# 格式 C：发送者名(12345) 2024-01-15 20:30:45
_MSG_C = re.compile(
    r"^(.+?)\((\d+)\)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*$"
)
# 检测用（不加 $ 锚定，适用于多行 head）
_MSG_C_DETECT = re.compile(
    r"^(.+?)\((\d+)\)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
)


def detect_qq_format(file_path: str) -> str:
    """检测 QQ 导出文件格式。"""
    p = Path(file_path)
    ext = p.suffix.lower()

    if ext == ".mht" or ext == ".mhtml":
        return "mht"

    if ext == ".txt":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
        if not head.strip():
            return "plaintext"
        # 格式 C 最特殊（名字+QQ号+时间戳），优先检测
        if _MSG_C_DETECT.search(head):
            return "qq_txt_c"
        if re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", head):
            if _MSG_A.search(head):
                return "qq_txt_a"
            return "qq_txt_b"
        return "plaintext"

    # 兜底：尝试当 txt 读
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        head = f.read(4000)
    if _MSG_A.search(head) or _MSG_B.search(head):
        return "qq_txt_a"
    return "plaintext"


def parse_qq_txt(file_path: str, target_name: str = "", fmt: str = "auto") -> list[dict]:
    """解析 QQ 导出的 TXT 文件。

    Args:
        file_path: 文件路径
        target_name: 对方昵称（用于标记 is_target）
        fmt: 格式标识，auto 时自动检测

    Returns:
        标准化消息列表
    """
    if fmt == "auto":
        fmt = detect_qq_format(file_path)

    if fmt == "mht":
        return _parse_mht(file_path, target_name)
    if fmt == "qq_txt_c":
        return _parse_txt_c(file_path, target_name)
    # qq_txt_a 和 qq_txt_b 共用同一逻辑（A 多了 QQ 号）
    return _parse_txt_ab(file_path, target_name)


def _parse_txt_ab(file_path: str, target_name: str) -> list[dict]:
    """解析格式 A（带 QQ 号）和格式 B（纯发送者名）。"""
    messages = []
    current_msg = None

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")

            # 尝试格式 A
            m = _MSG_A.match(line)
            if m:
                if current_msg:
                    messages.append(current_msg)
                timestamp, sender, qq = m.groups()
                current_msg = {
                    "timestamp": timestamp,
                    "sender": sender.strip(),
                    "qq": qq.strip(),
                    "content": "",
                }
                continue

            # 尝试格式 B（无 QQ 号）
            m = _MSG_B.match(line)
            if m:
                if current_msg:
                    messages.append(current_msg)
                timestamp, sender = m.groups()
                current_msg = {
                    "timestamp": timestamp,
                    "sender": sender.strip(),
                    "content": "",
                }
                continue

            # 消息内容行
            if current_msg and line.strip():
                if current_msg["content"]:
                    current_msg["content"] += "\n"
                current_msg["content"] += line

    if current_msg:
        messages.append(current_msg)

    return _normalize(messages, target_name)


def _parse_txt_c(file_path: str, target_name: str) -> list[dict]:
    """解析格式 C（发送者名 QQ号 时间戳）。"""
    messages = []
    current_msg = None

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.rstrip("\n")

            m = _MSG_C.match(line)
            if m:
                if current_msg:
                    messages.append(current_msg)
                sender, qq, timestamp = m.groups()
                current_msg = {
                    "timestamp": timestamp,
                    "sender": sender.strip(),
                    "qq": qq.strip(),
                    "content": "",
                }
                continue

            # 消息内容
            if current_msg and line.strip():
                if current_msg["content"]:
                    current_msg["content"] += "\n"
                current_msg["content"] += line

    if current_msg:
        messages.append(current_msg)

    return _normalize(messages, target_name)


def _parse_mht(file_path: str, target_name: str) -> list[dict]:
    """解析 MHT 格式（beta：提取文本部分）。"""
    try:
        import email
        from email import policy
    except ImportError:
        return []

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # MHT 是 MIME 格式，尝试解析
    try:
        msg = email.message_from_string(content, policy=policy.default)
    except Exception:
        return []

    # 找到 HTML 部分并提取文本
    text_parts = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct == "text/plain":
            payload = part.get_content()
            if payload:
                text_parts.append(payload)
        elif ct == "text/html":
            try:
                payload = part.get_content()
                if payload:
                    # 简单去除 HTML 标签
                    text = re.sub(r"<[^>]+>", "", payload)
                    text = re.sub(r"&nbsp;", " ", text)
                    text = re.sub(r"&[a-z]+;", "", text)
                    text_parts.append(text)
            except Exception:
                pass

    if not text_parts:
        return []

    # 合并后直接用字符串解析，避免临时文件
    merged = "\n".join(text_parts)
    return _parse_merged_text(merged, target_name)


def _parse_merged_text(text: str, target_name: str) -> list[dict]:
    """直接解析合并后的文本，不依赖临时文件。"""
    # 尝试各格式的正则匹配
    messages = []
    current_msg = None

    for line in text.splitlines():
        line = line.rstrip()

        # 尝试格式 C（名字+QQ号+时间戳）
        m = _MSG_C.match(line)
        if m:
            if current_msg:
                messages.append(current_msg)
            sender, qq, timestamp = m.groups()
            current_msg = {"timestamp": timestamp, "sender": sender.strip(), "qq": qq.strip(), "content": ""}
            continue

        # 尝试格式 A（时间戳+名字+QQ号）
        m = _MSG_A.match(line)
        if m:
            if current_msg:
                messages.append(current_msg)
            timestamp, sender, qq = m.groups()
            current_msg = {"timestamp": timestamp, "sender": sender.strip(), "qq": qq.strip(), "content": ""}
            continue

        # 尝试格式 B（时间戳+名字）
        m = _MSG_B.match(line)
        if m:
            if current_msg:
                messages.append(current_msg)
            timestamp, sender = m.groups()
            current_msg = {"timestamp": timestamp, "sender": sender.strip(), "content": ""}
            continue

        # 消息内容行
        if current_msg and line.strip():
            if current_msg["content"]:
                current_msg["content"] += "\n"
            current_msg["content"] += line

    if current_msg:
        messages.append(current_msg)

    return _normalize(messages, target_name)


def _normalize(messages: list[dict], target_name: str) -> list[dict]:
    """统一格式，标记 is_target。"""
    result = []
    # 收集所有发送者
    senders = {m["sender"] for m in messages if m.get("sender")}

    # 如果指定了 target_name，匹配发送者
    target_senders = set()
    if target_name:
        for s in senders:
            if target_name in s or s in target_name:
                target_senders.add(s)

    for m in messages:
        content = m.get("content", "").strip()
        if not content:
            continue

        sender = m.get("sender", "unknown")
        # 判断是否是目标对象
        if target_name:
            is_target = sender in target_senders
        else:
            # 未指定目标时，非"我"的都是 target
            is_target = sender != "我"

        result.append({
            "timestamp": m.get("timestamp", ""),
            "sender": sender,
            "content": content,
            "is_target": is_target,
        })

    return result


def parse(file_path: str, target_name: str = "", fmt: str = "auto") -> list[dict]:
    """统一入口：自动检测格式并解析。

    Returns:
        标准化消息列表，每条含 timestamp, sender, content, is_target
    """
    if fmt == "auto":
        fmt = detect_qq_format(file_path)

    if fmt == "plaintext":
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read().strip()
        if not content:
            return []
        return [{
            "timestamp": "",
            "sender": target_name or "unknown",
            "content": content,
            "is_target": True,
        }]

    return parse_qq_txt(file_path, target_name, fmt=fmt)
