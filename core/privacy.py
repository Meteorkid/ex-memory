"""隐私安全：敏感信息检测、数据脱敏、过期清理。"""

import fcntl
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

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
            result = pattern.sub(
                lambda m: m.group()[:3] + "****" + m.group()[-4:], result
            )
        elif name == "id_card":
            result = pattern.sub(
                lambda m: m.group()[:6] + "********" + m.group()[-4:], result
            )
        elif name == "bank_card":
            result = pattern.sub(
                lambda m: m.group()[:4] + " **** **** " + m.group()[-4:], result
            )
        elif name == "email":
            result = pattern.sub(
                lambda m: m.group()[0] + "***@" + m.group().split("@")[1], result
            )
    return result


def clean_expired_conversations(
    slug: str, retention_days: int = 90, owner: Optional[int] = None
) -> int:
    """清理过期的对话归档记录（按记录级 created_at 过滤后重写文件）。

    与 append_turn 共用同一把 .lock 文件锁，不会与并发写入互相破坏；
    解析失败或缺少时间戳的行保守保留。

    Returns:
        删除的记录条数
    """
    import json

    import config
    from core.file_utils import atomic_write, _lock

    conv_dir = config.resolve_ex_dir(slug, owner) / "conversations"
    if not conv_dir.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0

    for path in sorted(conv_dir.glob("*.jsonl")):
        lock_path = path.with_name(path.name + ".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            _lock(lock_file, fcntl.LOCK_EX)
            kept_lines: list[str] = []
            file_removed = 0
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(stripped)
                        created_at = datetime.fromisoformat(record["created_at"])
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                        # 无法判断时间的行保守保留
                        kept_lines.append(stripped)
                        continue
                    if created_at < cutoff:
                        file_removed += 1
                    else:
                        kept_lines.append(stripped)
            if file_removed:
                atomic_write(
                    path,
                    "\n".join(kept_lines) + "\n" if kept_lines else "",
                )
                removed += file_removed

    return removed


def scan_conversation(slug: str) -> dict:
    """扫描对话中的敏感信息。"""
    conv_file = Path(f"exes/{slug}/conversations/conversation.jsonl")
    if not conv_file.exists():
        return {"found": False, "types": [], "count": {}}

    total_found: dict[str, list] = {}
    total_count: dict[str, int] = {}

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
                            total_found.append(t) if isinstance(
                                total_found, list
                            ) else None
                    for t, c in result["count"].items():
                        total_count[t] = total_count.get(t, 0) + c
            except (json.JSONDecodeError, KeyError):
                pass

    return {
        "found": len(total_count) > 0,
        "types": list(total_count.keys()),
        "count": total_count,
    }
