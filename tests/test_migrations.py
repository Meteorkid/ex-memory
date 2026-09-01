"""数据库迁移：合规表与准入字段的建立、幂等性、增量升级。"""

import sqlite3

import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """指向一个全新的空库，并跑完全部迁移。"""
    import server.auth as auth

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()
    return db


def _tables(db):
    conn = sqlite3.connect(str(db))
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()


def _columns(db, table):
    conn = sqlite3.connect(str(db))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


class TestComplianceTables:
    def test_all_compliance_tables_created(self, fresh_db):
        assert {"consents", "safety_events", "subject_requests"} <= _tables(fresh_db)

    def test_user_guard_schema(self, fresh_db):
        assert "user_activity" in _tables(fresh_db)
        assert {"phone", "phone_verified_at", "age_confirmed_at"} <= _columns(
            fresh_db, "users"
        )

    def test_safety_events_has_no_plaintext_column(self, fresh_db):
        """安全事件表不得有存原话明文的字段——这是用户最脆弱时刻的记录。"""
        cols = _columns(fresh_db, "safety_events")
        for forbidden in ("content", "message", "raw_text", "user_input"):
            assert forbidden not in cols
        assert {"input_hash", "excerpt"} <= cols


class TestIdempotency:
    def test_init_db_twice_is_safe(self, fresh_db):
        """迁移已执行过时重复初始化不得报错——服务每次启动都会调用。"""
        import server.auth as auth

        auth.init_db()
        auth.init_db()
        assert {"consents", "user_activity"} <= _tables(fresh_db)

    def test_schema_version_advances_to_latest(self, fresh_db):
        from pathlib import Path

        expected = max(
            int(f.stem.split("_")[0]) for f in Path("migrations").glob("*.sql")
        )
        conn = sqlite3.connect(str(fresh_db))
        try:
            actual = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[
                0
            ]
        finally:
            conn.close()
        assert actual == expected


class TestIncrementalUpgrade:
    def test_upgrades_existing_v2_database_without_data_loss(
        self, tmp_path, monkeypatch
    ):
        """存量库（已跑到 002）升级时，用户数据必须完好。"""
        import server.auth as auth
        from pathlib import Path

        db = tmp_path / "users.db"
        monkeypatch.setattr(auth, "DB_PATH", db)
        monkeypatch.setattr(auth, "DB_DIR", db.parent)

        # 先只跑到 002，模拟存量库
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
        )
        for name in ("001_init.sql", "002_external_identities.sql"):
            conn.executescript((Path("migrations") / name).read_text(encoding="utf-8"))
        conn.execute("INSERT OR REPLACE INTO schema_version (version) VALUES (2)")
        conn.execute(
            "INSERT INTO users (username, password_hash, salt) VALUES ('存量用户', 'h', 's')"
        )
        conn.commit()
        conn.close()

        auth.init_db()

        assert {"consents", "safety_events", "user_activity"} <= _tables(db)
        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
            assert (
                conn.execute("SELECT username FROM users").fetchone()[0] == "存量用户"
            )
        finally:
            conn.close()
