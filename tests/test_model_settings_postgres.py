"""账号模型配置在任务专用 PostgreSQL 上的回归；绝不使用已有数据库。"""

import base64
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest


@pytest.fixture(scope="module")
def isolated_postgres():
    psycopg = pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    binaries = {}
    for name in ("initdb", "pg_ctl"):
        homebrew = Path("/opt/homebrew/bin") / name
        binary = str(homebrew) if homebrew.is_file() else shutil.which(name)
        if not binary:
            pytest.skip(f"缺少 {name}，不回退连接已有 PostgreSQL")
        binaries[name] = binary

    # macOS 的 pytest 默认根目录可能很长，socket 全路径不得超过 103 字节。
    temporary = tempfile.TemporaryDirectory(prefix="byok-pg-", dir="/tmp")
    root = Path(temporary.name)
    data = root / "data"
    sockets = root / "socket"
    sockets.mkdir(mode=0o700)
    initialized = False
    try:
        try:
            subprocess.run(
                [
                    binaries["initdb"],
                    "-D",
                    str(data),
                    "-U",
                    "byok_test",
                    "-A",
                    "trust",
                    "--no-locale",
                    "--encoding=UTF8",
                    "--nosync",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            initialized = True
            options = shlex.join(
                [
                    "-c",
                    "listen_addresses=",
                    "-c",
                    f"unix_socket_directories={sockets}",
                    "-c",
                    "unix_socket_permissions=0700",
                ]
            )
            subprocess.run(
                [
                    binaries["pg_ctl"],
                    "-D",
                    str(data),
                    "-l",
                    str(root / "postgres.log"),
                    "-o",
                    options,
                    "-w",
                    "-t",
                    "15",
                    "start",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            pytest.skip(
                f"无法启动任务专用 PostgreSQL（{type(exc).__name__}），不连接已有库"
            )

        conninfo = psycopg.conninfo.make_conninfo(
            host=str(sockets),
            port=5432,
            dbname="postgres",
            user="byok_test",
            connect_timeout=2,
            options="-cstatement_timeout=5000 -clock_timeout=3000",
        )
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations/postgres/006_user_model_settings.sql"
        )
        with psycopg.connect(conninfo, autocommit=True) as conn:
            assert conn.execute("SHOW listen_addresses").fetchone()[0] == ""
            conn.execute(
                (migration.parent / "001_schema.sql").read_text(encoding="utf-8")
            )
            conn.execute(migration.read_text(encoding="utf-8"))
        yield conninfo
    finally:
        if initialized and (data / "postmaster.pid").exists():
            subprocess.run(
                [
                    binaries["pg_ctl"],
                    "-D",
                    str(data),
                    "-m",
                    "immediate",
                    "-w",
                    "-t",
                    "15",
                    "stop",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
        temporary.cleanup()


@pytest.fixture
def pg_settings(isolated_postgres, monkeypatch):
    import config
    from core import db
    from server import model_settings
    from server.auth import _get_conn, register_user

    db.reset_pool()
    monkeypatch.setattr(config, "DATABASE_URL", isolated_postgres)
    monkeypatch.setattr(config, "DATABASE_POOL_SIZE", 4)
    monkeypatch.setattr(
        config,
        "MODEL_SETTINGS_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"p" * 32).decode(),
    )
    try:
        with _get_conn() as conn:
            conn.execute("TRUNCATE users RESTART IDENTITY CASCADE")
            conn.commit()
        assert register_user("pg_alice", "test-password") is None
        assert register_user("pg_bob", "test-password") is None
        yield model_settings
    finally:
        db.reset_pool()


def test_postgres_save_read_and_stale_revision(pg_settings):
    from server.auth import _get_conn

    settings = pg_settings
    secret = "sk-synthetic-postgres-key-1234"
    assert settings.get_public_settings(1)["revision"] == 0
    saved = settings.update_model_settings(1, 0, llm={"api_key": secret})
    assert saved["revision"] == 1
    assert saved["llm"]["key_last4"] == "1234"
    assert secret not in json.dumps(saved)
    assert "ciphertext" not in json.dumps(saved)
    assert settings.get_model_config(1, "llm")["api_key"] == secret
    assert settings.get_public_settings(2)["llm"]["has_key"] is False
    with _get_conn() as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM user_model_settings WHERE user_id = ?",
                (1,),
            ).fetchone()
        )
    assert secret not in json.dumps(row)
    with pytest.raises(settings.SettingsConflict):
        settings.update_model_settings(1, 0, llm={"delete_key": True})
    assert settings.get_settings_revision(1) == 1
    assert settings.get_model_config(1, "llm")["api_key"] == secret


def test_postgres_concurrent_saves_have_one_winner(pg_settings, monkeypatch):
    settings = pg_settings
    barrier = Barrier(2)
    original = settings._apply_update

    def apply_then_wait(*args):
        original(*args)
        barrier.wait(timeout=5)

    monkeypatch.setattr(settings, "_apply_update", apply_then_wait)

    def save(secret):
        try:
            return settings.update_model_settings(1, 0, llm={"api_key": secret})
        except settings.SettingsConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                save,
                (
                    "sk-synthetic-first-1111",
                    "sk-synthetic-second-2222",
                ),
            )
        )
    assert sum(result is not None for result in results) == 1
    assert settings.get_settings_revision(1) == 1


def test_postgres_embedding_lock_preserves_model_and_allows_rotation(pg_settings):
    settings = pg_settings
    settings.update_model_settings(
        1, 0, embedding={"api_key": "sk-synthetic-embed-1234"}
    )
    settings.lock_embedding_model(1, 1)
    assert settings.get_model_config(1, "embedding")["locked"] is True
    assert settings.get_settings_revision(1) == 1
    with pytest.raises(settings.EmbeddingModelLocked):
        settings.update_model_settings(1, 1, embedding={"model": "another-model"})
    settings.update_model_settings(
        1, 1, embedding={"api_key": "sk-synthetic-embed-5678"}
    )
    assert settings.get_model_config(1, "embedding")["api_key"].endswith("5678")
    with pytest.raises(settings.SettingsConflict):
        settings.lock_embedding_model(1, 1)


def test_postgres_delete_cascades_first_save_after_child_cleanup(pg_settings):
    from server.auth import _get_conn

    # 注销事务已清理空子表，另一个已认证请求在删 users 前首次保存。
    with _get_conn() as deleting:
        assert (
            deleting.execute(
                "DELETE FROM user_model_settings WHERE user_id = ?",
                (1,),
            ).rowcount
            == 0
        )
        pg_settings.update_model_settings(
            1, 0, llm={"api_key": "sk-synthetic-late-1234"}
        )
        assert (
            deleting.execute(
                "SELECT COUNT(*) AS n FROM user_model_settings WHERE user_id = ?",
                (1,),
            ).fetchone()["n"]
            == 1
        )
        deleting.execute("DELETE FROM users WHERE id = ?", (1,))
        deleting.commit()

    with _get_conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE id = ?", (1,)
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM user_model_settings WHERE user_id = ?",
                (1,),
            ).fetchone()["n"]
            == 0
        )


def test_postgres_account_deletion_removes_real_session_and_model_keys(
    pg_settings, monkeypatch, tmp_path
):
    import config
    import server.account_lifecycle as lifecycle
    from core.llm_router import _account_routers
    from server.auth import login_user_with_refresh, validate_token, refresh_session

    monkeypatch.setattr(
        lifecycle, "_feedback_path", lambda: tmp_path / "feedback.jsonl"
    )
    monkeypatch.setattr(
        lifecycle, "_sticker_dir", lambda uid: tmp_path / f"stickers-{uid}"
    )
    first = login_user_with_refresh("pg_alice", "test-password")
    other = login_user_with_refresh("pg_bob", "test-password")
    assert validate_token(first["token"]) == 1
    pg_settings.update_model_settings(
        1, 0, llm={"api_key": "sk-synthetic-delete-key-1234"}
    )
    for cache in (config._account_llm_clients, _account_routers):
        cache[(1, 1)] = object()
        cache[(2, 1)] = object()
    try:
        lifecycle.delete_account(1)
        assert lifecycle.verify_deletion(1) == []
        assert validate_token(first["token"]) is None
        assert refresh_session(first["refresh_token"]) is None
        assert validate_token(other["token"]) == 2
        assert pg_settings.get_public_settings(1)["llm"]["has_key"] is False
        for cache in (config._account_llm_clients, _account_routers):
            assert not any(key[0] == 1 for key in cache)
            assert any(key[0] == 2 for key in cache)
    finally:
        config._account_llm_clients.clear()
        _account_routers.clear()
