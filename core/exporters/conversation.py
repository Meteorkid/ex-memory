"""ex-memory 对话导出器：HTML / Markdown / JSON / TXT。"""

import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path

import config
from core.conversation_store import load_jsonl_messages

SUPPORTED_FORMATS = {"html", "md", "markdown", "json", "txt"}


class UnsupportedExportFormat(ValueError):
    """不支持的导出格式。"""


@dataclass(frozen=True)
class ExportedConversation:
    path: Path
    media_type: str
    filename: str


def export_ex_memory_conversation(slug: str, fmt: str = "html") -> ExportedConversation:
    """导出一个镜像的对话记录。"""
    fmt = _normalize_format(fmt)
    messages = load_conversation_messages(slug)
    content = _render(slug, messages, fmt)
    suffix = ".md" if fmt == "md" else f".{fmt}"
    tmp = tempfile.NamedTemporaryFile(prefix=f"ex-memory-{slug}-chat-", suffix=suffix, delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    tmp_path.write_text(content, encoding="utf-8")
    return ExportedConversation(
        path=tmp_path,
        media_type=_media_type(fmt),
        filename=f"ex-memory-{slug}-chat{suffix}",
    )


def load_conversation_messages(slug: str) -> list[dict]:
    """合并 Web/API JSONL 会话和 CLI Markdown 归档。"""
    messages = load_jsonl_messages(slug)
    messages.extend(_load_cli_session_messages(slug))
    return sorted(messages, key=lambda m: m.get("created_at", ""))


def _normalize_format(fmt: str) -> str:
    fmt = (fmt or "html").lower().strip()
    if fmt == "markdown":
        fmt = "md"
    if fmt not in SUPPORTED_FORMATS:
        raise UnsupportedExportFormat(f"不支持的导出格式: {fmt}")
    return fmt


def _media_type(fmt: str) -> str:
    return {
        "html": "text/html; charset=utf-8",
        "md": "text/markdown; charset=utf-8",
        "json": "application/json; charset=utf-8",
        "txt": "text/plain; charset=utf-8",
    }[fmt]


def _render(slug: str, messages: list[dict], fmt: str) -> str:
    if fmt == "html":
        return _render_html(slug, messages)
    if fmt == "md":
        return _render_markdown(slug, messages)
    if fmt == "json":
        return json.dumps({
            "slug": slug,
            "exported_at": datetime.now().isoformat(),
            "messages": messages,
        }, ensure_ascii=False, indent=2)
    return _render_text(slug, messages)


def _render_html(slug: str, messages: list[dict]) -> str:
    rows = []
    for msg in messages:
        role = msg.get("role", "")
        label = "我" if role == "user" else slug
        content = escape(str(msg.get("content", ""))).replace("\n", "<br>")
        created_at = escape(str(msg.get("created_at", "")))
        class_name = "user" if role == "user" else "assistant"
        rows.append(
            f'<article class="msg {class_name}">'
            f'<div class="meta"><span>{escape(label)}</span><time>{created_at}</time></div>'
            f'<div class="bubble">{content}</div>'
            "</article>"
        )
    body = "\n".join(rows) or '<p class="empty">暂无可导出的对话记录</p>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(slug)} - ex-memory 对话导出</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f5f5f7; color: #1d1d1f; }}
    header {{ position: sticky; top: 0; padding: 16px 20px; background: rgba(255,255,255,.92); border-bottom: 1px solid #ddd; backdrop-filter: blur(12px); }}
    h1 {{ margin: 0; font-size: 18px; }}
    main {{ max-width: 840px; margin: 0 auto; padding: 24px 16px 40px; }}
    .msg {{ display: flex; flex-direction: column; margin: 14px 0; }}
    .msg.user {{ align-items: flex-end; }}
    .msg.assistant {{ align-items: flex-start; }}
    .meta {{ margin: 0 8px 4px; font-size: 12px; color: #777; display: flex; gap: 8px; }}
    .bubble {{ max-width: min(680px, 82vw); padding: 10px 13px; border-radius: 8px; line-height: 1.55; white-space: normal; word-break: break-word; }}
    .user .bubble {{ background: #95ec69; }}
    .assistant .bubble {{ background: #fff; border: 1px solid #e6e6e6; }}
    .empty {{ color: #777; text-align: center; padding: 48px 0; }}
  </style>
</head>
<body>
  <header><h1>{escape(slug)} - ex-memory 对话导出</h1></header>
  <main>{body}</main>
</body>
</html>
"""


def _render_markdown(slug: str, messages: list[dict]) -> str:
    lines = [f"# {slug} - ex-memory 对话导出", ""]
    for msg in messages:
        label = "我" if msg.get("role") == "user" else slug
        created_at = msg.get("created_at", "")
        lines.append(f"## {label} {created_at}".strip())
        lines.append("")
        lines.append(str(msg.get("content", "")))
        lines.append("")
    return "\n".join(lines)


def _render_text(slug: str, messages: list[dict]) -> str:
    lines = [f"{slug} - ex-memory 对话导出", ""]
    for msg in messages:
        label = "我" if msg.get("role") == "user" else slug
        created_at = msg.get("created_at", "")
        lines.append(f"[{created_at}] {label}: {msg.get('content', '')}")
    return "\n".join(lines)


def _load_cli_session_messages(slug: str) -> list[dict]:
    sessions_dir = config.get_ex_dir(slug) / "sessions"
    if not sessions_dir.exists():
        return []

    messages = []
    for path in sorted(sessions_dir.glob("session_*.md")):
        if path.name.endswith("_summary.md"):
            continue
        session_time = _session_timestamp(path)
        for role, content in _parse_cli_session(path.read_text(encoding="utf-8")):
            messages.append({
                "id": f"{path.stem}-{len(messages)}",
                "role": role,
                "content": content,
                "created_at": session_time,
                "source": "cli",
            })
    return messages


def _parse_cli_session(text: str) -> list[tuple[str, str]]:
    records = []
    pattern = re.compile(r"^\*\*(?P<label>[^*]+)\*\*:\s*(?P<content>.*)$")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        label = match.group("label")
        role = "user" if label == "用户" else "assistant"
        records.append((role, match.group("content")))
    return records


def _session_timestamp(path: Path) -> str:
    match = re.search(r"session_(\d{8}_\d{6})", path.stem)
    if not match:
        return ""
    try:
        return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").isoformat()
    except ValueError:
        return ""
