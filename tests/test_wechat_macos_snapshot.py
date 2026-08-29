import hashlib
from pathlib import Path

import pytest

from local_helper.wechat_macos.snapshot import create_database_snapshot


def test_snapshot_copies_db_wal_and_shm_without_changing_source(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    db = source / "message.db"
    db.write_bytes(b"database")
    (source / "message.db-wal").write_bytes(b"wal")
    (source / "message.db-shm").write_bytes(b"shm")
    original_hash = hashlib.sha256(db.read_bytes()).hexdigest()

    files = create_database_snapshot(db, tmp_path / "snapshot")

    assert {item.snapshot.name for item in files} == {
        "message.db",
        "message.db-wal",
        "message.db-shm",
    }
    assert hashlib.sha256(db.read_bytes()).hexdigest() == original_hash
    assert all(item.snapshot.read_bytes() == item.source.read_bytes() for item in files)


def test_snapshot_refuses_to_overwrite_existing_file(tmp_path: Path):
    db = tmp_path / "message.db"
    db.write_bytes(b"database")
    destination = tmp_path / "snapshot"
    destination.mkdir()
    (destination / db.name).write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        create_database_snapshot(db, destination)
