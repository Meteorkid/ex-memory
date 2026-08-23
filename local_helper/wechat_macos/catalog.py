"""从解密快照中识别联系人、会话和消息数据库。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from local_helper.wechat_macos.reader import Contact, Session, read_contacts, read_sessions


@dataclass(frozen=True)
class DecryptedCatalog:
    contact_databases: tuple[Path, ...]
    session_databases: tuple[Path, ...]
    message_databases: tuple[Path, ...]
    media_databases: tuple[Path, ...]


def discover_decrypted_catalog(databases: tuple[Path, ...]) -> DecryptedCatalog:
    contacts: list[Path] = []
    sessions: list[Path] = []
    messages: list[Path] = []
    media: list[Path] = []
    for database in databases:
        tables = _read_table_names(database)
        if tables.intersection({"contact", "Friend"}):
            contacts.append(database)
        if tables.intersection({"SessionTable", "SessionAbstract", "Session"}):
            sessions.append(database)
        if any(re.fullmatch(r"(?:Msg|Chat)_[0-9A-Fa-f]{32}", table) for table in tables):
            messages.append(database)
        if "VoiceInfo" in tables and "Name2Id" in tables:
            media.append(database)
    return DecryptedCatalog(tuple(contacts), tuple(sessions), tuple(messages), tuple(media))


def load_session_catalog(catalog: DecryptedCatalog) -> tuple[dict[str, Contact], tuple[Session, ...]]:
    contacts: dict[str, Contact] = {}
    for database in catalog.contact_databases:
        contacts.update(read_contacts(database))
    sessions: dict[str, Session] = {}
    for database in catalog.session_databases:
        for session in read_sessions(database, contacts):
            previous = sessions.get(session.wxid)
            if previous is None or session.last_timestamp > previous.last_timestamp:
                sessions[session.wxid] = session
    return contacts, tuple(sorted(sessions.values(), key=lambda item: item.last_timestamp, reverse=True))


def _read_table_names(database: Path) -> set[str]:
    if database.is_symlink():
        raise ValueError("数据库不能是符号链接")
    resolved = database.resolve(strict=True)
    with sqlite3.connect(f"file:{resolved}?mode=ro", uri=True) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
