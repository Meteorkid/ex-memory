import sqlite3
import subprocess
from pathlib import Path

import pytest

from local_helper.wechat_macos.discovery import WeChatAccount
from local_helper.wechat_macos.key_extractor import KeyExtractionError, WeChatKeyPair
from local_helper.wechat_macos.pipeline import decrypt_account_databases, find_wechat_pid


def test_find_wechat_pid_uses_exact_process_name():
    def runner(command, **kwargs):
        assert command == ["/usr/bin/pgrep", "-x", "WeChat"]
        return subprocess.CompletedProcess(command, 0, "456\n123\n", "")

    assert find_wechat_pid(runner) == 123


def test_plain_sqlite_database_is_snapshotted_without_key(tmp_path: Path):
    storage = tmp_path / "db_storage"
    source = storage / "message" / "msg.db"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE message(id INTEGER PRIMARY KEY)")
    account = WeChatAccount("account", tmp_path, storage, (source,), "fingerprint")

    outputs = decrypt_account_databases(
        account=account,
        task_dir=tmp_path / "task",
        keys=(),
        sqlcipher_binary=tmp_path / "unused",
    )

    assert outputs[0].is_file()
    with sqlite3.connect(outputs[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_encrypted_database_requires_matching_salt(tmp_path: Path):
    storage = tmp_path / "db_storage"
    source = storage / "message" / "msg.db"
    source.parent.mkdir(parents=True)
    source.write_bytes(bytes.fromhex("11" * 16) + b"encrypted")
    account = WeChatAccount("account", tmp_path, storage, (source,), "fingerprint")

    with pytest.raises(KeyExtractionError, match="缺少"):
        decrypt_account_databases(
            account=account,
            task_dir=tmp_path / "task",
            keys=(WeChatKeyPair(key_hex="22" * 32, salt_hex="33" * 16),),
            sqlcipher_binary=tmp_path / "unused",
        )
