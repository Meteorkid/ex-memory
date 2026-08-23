import sqlite3
from pathlib import Path

from local_helper.wechat_macos.catalog import discover_decrypted_catalog, load_session_catalog


def _database(path: Path, statements: tuple[str, ...]) -> Path:
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)
    return path


def test_catalog_discovers_and_loads_v4_databases(tmp_path: Path):
    contact = _database(
        tmp_path / "contact.db",
        (
            "CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT, local_type INTEGER)",
            "INSERT INTO contact VALUES ('wxid_friend', '昵称', '备注', 1)",
        ),
    )
    session = _database(
        tmp_path / "session.db",
        (
            "CREATE TABLE SessionTable(username TEXT, last_timestamp INTEGER, nMsgCount INTEGER)",
            "INSERT INTO SessionTable VALUES ('wxid_friend', 123, 7)",
        ),
    )
    message = _database(
        tmp_path / "message.db",
        ("CREATE TABLE Msg_0123456789abcdef0123456789abcdef(local_id INTEGER)",),
    )

    catalog = discover_decrypted_catalog((contact, session, message))
    contacts, sessions = load_session_catalog(catalog)

    assert catalog.message_databases == (message,)
    assert contacts["wxid_friend"].display_name == "备注"
    assert sessions[0].display_name == "备注"
    assert sessions[0].message_count == 7
