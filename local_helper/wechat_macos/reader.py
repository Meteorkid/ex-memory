"""已解密 macOS 微信数据库的只读适配器。"""

from __future__ import annotations

import hashlib
import heapq
import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator


class UnsupportedSchemaError(RuntimeError):
    """数据库 schema 未经适配。"""


class MessageDecodeError(RuntimeError):
    """消息载荷无法安全解码。"""


class MessageKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    EMOJI = "emoji"
    LINK = "link"
    MUSIC = "music"
    LOCATION = "location"
    RED_PACKET = "red_packet"
    TRANSFER = "transfer"
    POKE = "poke"
    CALL = "call"
    SHARE = "share"
    REPLY = "reply"
    FORWARD = "forward"
    CONTACT = "contact"
    SYSTEM = "system"
    RECALL = "recall"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Contact:
    wxid: str
    display_name: str
    remark: str = ""
    nickname: str = ""
    local_type: int = 0


@dataclass(frozen=True)
class Session:
    wxid: str
    display_name: str
    is_group: bool
    last_timestamp: int = 0
    message_count: int = 0


@dataclass(frozen=True)
class Message:
    local_id: int
    create_time: int
    content: str
    direction: int
    raw_type: int
    app_subtype: int | None
    sender_wxid: str
    kind: MessageKind
    shard: str
    server_id: int = 0
    sort_seq: int = 0
    resource_content: str = ""


def read_contacts(database: Path) -> dict[str, Contact]:
    with _connect_readonly(database) as connection:
        tables = _table_names(connection)
        if "contact" in tables:
            columns = _column_names(connection, "contact")
            if "username" not in columns:
                raise UnsupportedSchemaError("contact 表缺少用户标识字段")
            nickname_column = _first_existing(columns, "nick_name", "nickname")
            remark_column = _first_existing(columns, "remark", "con_remark")
            alias_column = _first_existing(columns, "alias", "alias_name")
            type_column = _first_existing(columns, "local_type", "type")
            rows = connection.execute(
                f'SELECT "username", {_quoted_or_default(nickname_column, "NULL")} AS nickname, '
                f'{_quoted_or_default(remark_column, "NULL")} AS remark, '
                f'{_quoted_or_default(alias_column, "NULL")} AS alias_name, '
                f'{_quoted_or_default(type_column, "0")} AS local_type '
                'FROM "contact" WHERE "username" IS NOT NULL'
            )
            return {
                row["username"]: Contact(
                    wxid=row["username"],
                    display_name=row["remark"] or row["nickname"] or row["alias_name"] or row["username"],
                    remark=row["remark"] or "",
                    nickname=row["nickname"] or "",
                    local_type=int(row["local_type"] or 0),
                )
                for row in rows
                if isinstance(row["username"], str) and row["username"]
            }
        if "Friend" in tables:
            return _read_v3_contacts(connection)
        raise UnsupportedSchemaError("未找到可支持的联系人表")


def read_sessions(database: Path, contacts: dict[str, Contact]) -> tuple[Session, ...]:
    with _connect_readonly(database) as connection:
        tables = _table_names(connection)
        table = next((name for name in ("SessionTable", "SessionAbstract", "Session") if name in tables), None)
        if not table:
            raise UnsupportedSchemaError("未找到可支持的会话表")
        columns = _column_names(connection, table)
        wxid_column = _first_existing(columns, "username", "ConStrRes1", "userName")
        timestamp_column = _first_existing(
            columns,
            "last_timestamp",
            "sort_timestamp",
            "last_time",
            "sort_seq",
            "nOrder",
        )
        count_column = _first_existing(columns, "nMsgCount", "message_count")
        if not wxid_column:
            raise UnsupportedSchemaError("会话表缺少用户标识字段")
        select = (
            f'SELECT "{wxid_column}" AS wxid, '
            f'{_quoted_or_default(timestamp_column, "0")} AS timestamp, '
            f'{_quoted_or_default(count_column, "0")} AS message_count '
            f'FROM "{table}"'
        )
        if timestamp_column:
            select += f' ORDER BY "{timestamp_column}" DESC'
        sessions: list[Session] = []
        for row in connection.execute(select):
            wxid = row["wxid"]
            if not isinstance(wxid, str) or not wxid or _skip_session(wxid):
                continue
            contact = contacts.get(wxid)
            sessions.append(
                Session(
                    wxid=wxid,
                    display_name=contact.display_name if contact else wxid,
                    is_group=wxid.endswith("@chatroom"),
                    last_timestamp=int(row["timestamp"] or 0),
                    message_count=int(row["message_count"] or 0),
                )
            )
        return tuple(sessions)


def iter_messages(
    databases: Iterable[Path],
    *,
    session_wxid: str,
    owner_wxid: str,
) -> Iterator[Message]:
    candidates = _message_table_candidates(session_wxid)
    streams = [
        _iter_database_messages(database, candidates, session_wxid, owner_wxid)
        for database in sorted(databases, key=lambda path: path.name)
    ]
    yield from heapq.merge(
        *streams,
        key=lambda message: (message.create_time, message.sort_seq, message.local_id),
    )


def decode_message_content(raw: bytes | str | None, compression_type: int | None) -> str:
    if raw is None:
        return ""
    payload = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    if len(payload) > 32 * 1024 * 1024:
        raise MessageDecodeError("单条消息载荷超过安全限制")
    if compression_type == 4:
        try:
            import zstandard
        except ImportError as exc:
            raise MessageDecodeError("缺少 zstandard，无法解码微信压缩消息") from exc
        try:
            payload = zstandard.ZstdDecompressor().decompress(payload, max_output_size=32 * 1024 * 1024)
        except zstandard.ZstdError as exc:
            raise MessageDecodeError("zstd 消息解压失败") from exc
    return payload.decode("utf-8", errors="replace")


def map_message_kind(raw_type: int, app_subtype: int | None = None) -> MessageKind:
    direct = {
        1: MessageKind.TEXT,
        3: MessageKind.IMAGE,
        34: MessageKind.VOICE,
        42: MessageKind.CONTACT,
        43: MessageKind.VIDEO,
        47: MessageKind.EMOJI,
        48: MessageKind.LOCATION,
        50: MessageKind.CALL,
        10000: MessageKind.SYSTEM,
        10002: MessageKind.RECALL,
    }
    if raw_type in direct:
        return direct[raw_type]
    if raw_type != 49:
        return MessageKind.UNKNOWN
    subtype_map = {
        3: MessageKind.MUSIC,
        4: MessageKind.LINK,
        5: MessageKind.LINK,
        6: MessageKind.FILE,
        8: MessageKind.EMOJI,
        19: MessageKind.FORWARD,
        33: MessageKind.SHARE,
        36: MessageKind.SHARE,
        51: MessageKind.SHARE,
        57: MessageKind.REPLY,
        2000: MessageKind.TRANSFER,
        2001: MessageKind.RED_PACKET,
    }
    return subtype_map.get(app_subtype, MessageKind.LINK)


def infer_app_subtype(content: str) -> int | None:
    if len(content) > 8 * 1024 * 1024:
        return None
    match = re.search(r"<type>\s*(\d{1,5})\s*</type>", content)
    return int(match.group(1)) if match else None


def _read_message_table(
    connection: sqlite3.Connection,
    table: str,
    session_wxid: str,
    owner_wxid: str,
    shard: str,
) -> Iterator[Message]:
    columns = _column_names(connection, table)
    if {"message_content", "local_type", "local_id", "create_time"}.issubset(columns):
        yield from _read_v4_messages(connection, table, session_wxid, owner_wxid, shard, columns)
        return
    if {"MesLocalID", "CreateTime", "Message", "Des", "Type"}.issubset(columns):
        yield from _read_v3_messages(connection, table, session_wxid, owner_wxid, shard, columns)
        return
    raise UnsupportedSchemaError(f"消息表 schema 未支持: {table}")


def _read_v4_messages(
    connection: sqlite3.Connection,
    table: str,
    session_wxid: str,
    owner_wxid: str,
    shard: str,
    columns: set[str],
) -> Iterator[Message]:
    compression_column = '"WCDB_CT_message_content"' if "WCDB_CT_message_content" in columns else "NULL"
    sender_column = '"real_sender_id"' if "real_sender_id" in columns else "NULL"
    server_column = '"server_id"' if "server_id" in columns else "0"
    sort_column = '"sort_seq"' if "sort_seq" in columns else '"create_time"'
    resource_column = '"packed_info_data"' if "packed_info_data" in columns else "NULL"
    resource_compression = '"WCDB_CT_packed_info_data"' if "WCDB_CT_packed_info_data" in columns else "NULL"
    query = (
        f'SELECT "local_id", "local_type", "create_time", "message_content", '
        f'{compression_column} AS compression_type, {sender_column} AS real_sender_id, '
        f'{server_column} AS server_id, {sort_column} AS sort_seq, '
        f'{resource_column} AS resource_content, {resource_compression} AS resource_compression '
        f'FROM "{table}" ORDER BY "create_time", {sort_column}, "local_id"'
    )
    is_group = session_wxid.endswith("@chatroom")
    for row in connection.execute(query):
        content = decode_message_content(row["message_content"], row["compression_type"])
        sender_id = int(row["real_sender_id"] or 0)
        sent = sender_id == 2
        sender = owner_wxid if sent else session_wxid
        if is_group:
            prefixed_sender, content = _split_group_sender(content)
            if not sent:
                sender = prefixed_sender or "unknown"
        resource_content = decode_message_content(row["resource_content"], row["resource_compression"])
        subtype = infer_app_subtype(content) if int(row["local_type"]) == 49 else None
        yield Message(
            local_id=int(row["local_id"]),
            create_time=int(row["create_time"]),
            content=content,
            direction=1 if sent else 0,
            raw_type=int(row["local_type"]),
            app_subtype=subtype,
            sender_wxid=sender,
            kind=map_message_kind(int(row["local_type"]), subtype),
            shard=shard,
            server_id=int(row["server_id"] or 0),
            sort_seq=int(row["sort_seq"] or 0),
            resource_content=resource_content,
        )


def _read_v3_messages(
    connection: sqlite3.Connection,
    table: str,
    session_wxid: str,
    owner_wxid: str,
    shard: str,
    columns: set[str],
) -> Iterator[Message]:
    subtype = '"SubType"' if "SubType" in columns else "NULL"
    query = (
        f'SELECT "MesLocalID", "CreateTime", "Message", "Des", "Type", {subtype} AS subtype '
        f'FROM "{table}" ORDER BY "CreateTime", "MesLocalID"'
    )
    is_group = session_wxid.endswith("@chatroom")
    for row in connection.execute(query):
        content = row["Message"] or ""
        direction = int(row["Des"] or 0)
        if direction == 1:
            sender = owner_wxid
        elif is_group:
            sender, content = _split_group_sender(content)
            sender = sender or "unknown"
        else:
            sender = session_wxid
        app_subtype = int(row["subtype"]) if row["subtype"] is not None else infer_app_subtype(content)
        yield Message(
            local_id=int(row["MesLocalID"]),
            create_time=int(row["CreateTime"]),
            content=content,
            direction=direction,
            raw_type=int(row["Type"]),
            app_subtype=app_subtype,
            sender_wxid=sender,
            kind=map_message_kind(int(row["Type"]), app_subtype),
            shard=shard,
        )


def _connect_readonly(database: Path) -> sqlite3.Connection:
    if database.is_symlink():
        raise ValueError("数据库必须是普通文件")
    database = database.resolve(strict=True)
    if not database.is_file():
        raise ValueError("数据库必须是普通文件")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _iter_database_messages(
    database: Path,
    candidates: tuple[str, ...],
    session_wxid: str,
    owner_wxid: str,
) -> Iterator[Message]:
    with _connect_readonly(database) as connection:
        table = next((candidate for candidate in candidates if candidate in _table_names(connection)), None)
        if table:
            yield from _read_message_table(connection, table, session_wxid, owner_wxid, database.name)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise UnsupportedSchemaError("数据表名称无效")
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _message_table_candidates(wxid: str) -> tuple[str, ...]:
    digest = hashlib.md5(wxid.encode("utf-8"), usedforsecurity=False).hexdigest()
    return (f"Msg_{digest}", f"Msg_{digest.upper()}", f"Chat_{digest}", f"Chat_{digest.upper()}")


def _split_group_sender(content: str) -> tuple[str, str]:
    delimiter = content.find(":\n")
    if 0 < delimiter < 96:
        candidate = content[:delimiter]
        if re.fullmatch(r"[A-Za-z0-9_@.\-]+", candidate):
            return candidate, content[delimiter + 2 :]
    return "", content


def _skip_session(wxid: str) -> bool:
    return wxid in {"filehelper", "fmessage", "notifymessage", "weixin"} or wxid.startswith("gh_")


def _first_existing(columns: set[str], *candidates: str) -> str | None:
    return next((candidate for candidate in candidates if candidate in columns), None)


def _quoted_or_default(column: str | None, default: str) -> str:
    return f'"{column}"' if column else default


def _read_v3_contacts(connection: sqlite3.Connection) -> dict[str, Contact]:
    columns = _column_names(connection, "Friend")
    if not {"userName", "type"}.issubset(columns):
        raise UnsupportedSchemaError("Friend 表缺少必要字段")
    # 3.x protobuf 备注/昵称将在对应 schema 固件中扩展；先保留 wxid 以避免丢会话。
    return {
        row["userName"]: Contact(
            wxid=row["userName"],
            display_name=row["userName"],
            local_type=int(row["type"] or 0),
        )
        for row in connection.execute('SELECT "userName", "type" FROM "Friend" WHERE "userName" IS NOT NULL')
        if isinstance(row["userName"], str) and row["userName"]
    }
