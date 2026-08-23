import hashlib
import sqlite3
from pathlib import Path

from local_helper.wechat_macos.reader import (
    MessageKind,
    infer_app_subtype,
    iter_messages,
    map_message_kind,
    read_contacts,
    read_sessions,
)


def test_read_contacts_and_sessions_v4(tmp_path: Path):
    contact_db = tmp_path / "contact.sqlite"
    with sqlite3.connect(contact_db) as connection:
        connection.execute(
            "CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT, local_type INTEGER)"
        )
        connection.execute("INSERT INTO contact VALUES (?, ?, ?, ?)", ("wxid_friend", "昵称", "备注", 3))
    contacts = read_contacts(contact_db)
    assert contacts["wxid_friend"].display_name == "备注"

    session_db = tmp_path / "session.sqlite"
    with sqlite3.connect(session_db) as connection:
        connection.execute(
            "CREATE TABLE SessionTable(username TEXT, last_timestamp INTEGER, message_count INTEGER)"
        )
        connection.execute("INSERT INTO SessionTable VALUES (?, ?, ?)", ("wxid_friend", 123, 9))
        connection.execute("INSERT INTO SessionTable VALUES (?, ?, ?)", ("filehelper", 124, 1))

    sessions = read_sessions(session_db, contacts)
    assert len(sessions) == 1
    assert sessions[0].display_name == "备注"
    assert sessions[0].message_count == 9


def test_contact_and_session_schema_optional_columns(tmp_path: Path):
    contact_db = tmp_path / "contact.sqlite"
    with sqlite3.connect(contact_db) as connection:
        connection.execute("CREATE TABLE contact(username TEXT, alias TEXT)")
        connection.execute("INSERT INTO contact VALUES ('wxid_friend', 'wechat_alias')")
    contacts = read_contacts(contact_db)
    assert contacts["wxid_friend"].display_name == "wechat_alias"

    session_db = tmp_path / "session.sqlite"
    with sqlite3.connect(session_db) as connection:
        connection.execute("CREATE TABLE SessionTable(username TEXT, last_time INTEGER)")
        connection.execute("INSERT INTO SessionTable VALUES ('wxid_friend', 456)")
    sessions = read_sessions(session_db, contacts)
    assert sessions[0].last_timestamp == 456


def test_iter_messages_v4_orders_shards_and_marks_sender_id_two_as_self(tmp_path: Path):
    session_wxid = "wxid_friend"
    owner_wxid = "wxid_owner"
    table = "Msg_" + hashlib.md5(session_wxid.encode(), usedforsecurity=False).hexdigest()
    shards = []
    for index, rows in enumerate((((2, 200, b"second"),), ((1, 100, b"first"),))):
        database = tmp_path / f"message_{index}.sqlite"
        shards.append(database)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
            connection.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, ?)", (owner_wxid,))
            connection.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (2, ?)", (session_wxid,))
            connection.execute(
                f'CREATE TABLE "{table}"('
                "local_id INTEGER, local_type INTEGER, create_time INTEGER, message_content BLOB, "
                "WCDB_CT_message_content INTEGER, real_sender_id INTEGER)"
            )
            for local_id, create_time, content in rows:
                connection.execute(
                    f'INSERT INTO "{table}" VALUES (?, 1, ?, ?, 0, 2)',
                    (local_id, create_time, content),
                )

    messages = tuple(iter_messages(shards, session_wxid=session_wxid, owner_wxid=owner_wxid))

    assert [message.content for message in messages] == ["first", "second"]
    assert all(message.sender_wxid == owner_wxid for message in messages)
    assert all(message.direction == 1 for message in messages)
    assert all(message.kind is MessageKind.TEXT for message in messages)


def test_group_sender_prefix_and_app_subtype(tmp_path: Path):
    session_wxid = "group@chatroom"
    owner_wxid = "wxid_owner"
    table = "Msg_" + hashlib.md5(session_wxid.encode(), usedforsecurity=False).hexdigest()
    database = tmp_path / "message_0.sqlite"
    content = "wxid_member:\n<appmsg><type>6</type><title>report.pdf</title></appmsg>"
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'CREATE TABLE "{table}"('
            "local_id INTEGER, local_type INTEGER, create_time INTEGER, message_content BLOB)"
        )
        connection.execute(f'INSERT INTO "{table}" VALUES (1, 49, 100, ?)', (content.encode(),))

    message = next(iter_messages((database,), session_wxid=session_wxid, owner_wxid=owner_wxid))

    assert message.sender_wxid == "wxid_member"
    assert message.app_subtype == 6
    assert message.kind is MessageKind.FILE


def test_message_type_mapping_has_unknown_fallback():
    assert infer_app_subtype("<appmsg><type>57</type></appmsg>") == 57
    assert map_message_kind(49, 57) is MessageKind.REPLY
    assert map_message_kind(999999) is MessageKind.UNKNOWN
