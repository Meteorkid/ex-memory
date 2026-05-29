"""隐私安全：敏感信息检测、数据脱敏、过期清理。"""

import re
from datetime import datetime, timedelta
from pathlib import Path

# 敏感信息正则
PATTERNS = {
    "phone": re.compile(r"1[3-9]\d{9}"),
    "id_card": re.compile(r"\d{17}[\dXx]"),
    "bank_card": re.compile(r"\d{16,19}"),
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
}


def scan_sensitive(text: str) -> dict:
    """扫描文本中的敏感信息。

    Returns:
        {"found": bool, "types": ["phone", "id_card"], "count": {"phone": 2}}
    """
    found_types = []
    counts = {}

    for name, pattern in PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            found_types.append(name)
            counts[name] = len(matches)

    return {
        "found": len(found_types) > 0,
        "types": found_types,
        "count": counts,
    }


def mask_sensitive(text: str) -> str:
    """脱敏处理：将敏感信息替换为 ***。"""
    result = text
    for name, pattern in PATTERNS.items():
        if name == "phone":
            result = pattern.sub(lambda m: m.group()[:3] + "****" + m.group()[-4:], result)
        elif name == "id_card":
            result = pattern.sub(lambda m: m.group()[:6] + "********" + m.group()[-4:], result)
        elif name == "bank_card":
            result = pattern.sub(lambda m: m.group()[:4] + " **** **** " + m.group()[-4:], result)
        elif name == "email":
            result = pattern.sub(lambda m: m.group()[0] + "***@" + m.group().split("@")[1], result)
    return result


def clean_expired_conversations(slug: str, retention_days: int = 90) -> int:
    """清理过期的对话归档文件。

    Returns:
        删除的文件数量
    """
    conv_dir = Path(f"exes/{slug}/conversations")
    if not conv_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = 0

    for f in conv_dir.glob("*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                f.unlink()
                deleted += 1
        except (OSError, ValueError):
            pass

    return deleted


def scan_conversation(slug: str) -> dict:
    """扫描对话中的敏感信息。"""
    conv_file = Path(f"exes/{slug}/conversations/conversation.jsonl")
    if not conv_file.exists():
        return {"found": False, "types": [], "count": {}}

    total_found = {}
    total_count = {}

    with open(conv_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                import json
                msg = json.loads(line)
                content = msg.get("content", "")
                result = scan_sensitive(content)
                if result["found"]:
                    for t in result["types"]:
                        if t not in total_found:
                            total_found.append(t) if isinstance(total_found, list) else None
                    for t, c in result["count"].items():
                        total_count[t] = total_count.get(t, 0) + c
            except (json.JSONDecodeError, KeyError):
                pass

    return {
        "found": len(total_count) > 0,
        "types": list(total_count.keys()),
        "count": total_count,
    }
