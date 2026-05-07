"""口述/粘贴文本解析器：将用户直接输入的文本转为标准消息格式。"""


def parse(text: str, target_name: str = "") -> list[dict]:
    """将口述/粘贴文本转为标准消息列表。

    如果文本中有明确的对话格式（如 "张三: xxx" 或 "我: xxx"），尝试解析。
    否则整段作为一条来自 target 的消息。
    """
    if not text.strip():
        return []

    lines = text.strip().split("\n")
    messages = []

    # 尝试解析 "发送者: 内容" 格式
    has_dialog_format = False
    for line in lines:
        if ":" in line or "：" in line:
            has_dialog_format = True
            break

    if has_dialog_format:
        import re
        pattern = re.compile(r"^(.+?)[：:]\s*(.+)$")
        for line in lines:
            match = pattern.match(line.strip())
            if match:
                sender, content = match.groups()
                is_me = sender.strip() in ("我", "我方", "user", "me")
                messages.append({
                    "timestamp": "",
                    "sender": "我" if is_me else sender.strip(),
                    "content": content.strip(),
                    "is_target": not is_me,
                })
            elif line.strip():
                # 非对话行作为上下文补充
                messages.append({
                    "timestamp": "",
                    "sender": target_name or "context",
                    "content": line.strip(),
                    "is_target": True,
                })
    else:
        # 整段作为一条消息
        messages.append({
            "timestamp": "",
            "sender": target_name or "narrative",
            "content": text.strip(),
            "is_target": True,
        })

    return messages
