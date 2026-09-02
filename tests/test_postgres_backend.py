"""同一套业务代码跑在真实 PostgreSQL 上（FR-030）。

SQLite 单文件在多副本下是错的：多个写入方共享一个文件会锁冲突甚至损坏。
FR-035 把状态搬出进程之后，数据库就是横向扩展的最后一个阻塞点。

本机无 Postgres 时整组跳过，不让 CI 因为缺中间件变红。
"""

import os
import uuid

import pytest

PG_URL = os.getenv("TEST_DATABASE_URL", "postgresql:///ex_memory_dev")


def _pg_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(PG_URL, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="本机无可用 PostgreSQL")


@pytest.fixture
def pg(monkeypatch):
    """把整个应用切到 Postgres，并在独立 schema 里跑，跑完清掉。"""
    import psycopg

    from core import db

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")

    # search_path 带上 public：vector 扩展装在 public，
    # 不带的话建表时找不到 vector 类型
    url = f"{PG_URL}?options=-csearch_path%3D{schema},public"
    monkeypatch.setattr("config.DATABASE_URL", url)
    db.reset_pool()

    from server.auth import init_db

    init_db()
    yield schema

    db.reset_pool()
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


class TestDialect:
    def test_dialect_follows_config(self, pg):
        from core import db

        assert db.dialect() == db.DIALECT_POSTGRES

    def test_placeholder_translation(self, pg):
        from core.db import _translate

        assert _translate("SELECT * FROM t WHERE a = ? AND b = ?") == (
            "SELECT * FROM t WHERE a = %s AND b = %s"
        )

    def test_question_mark_inside_string_literal_is_left_alone(self, pg):
        from core.db import _translate

        out = _translate("SELECT '真的吗?' WHERE a = ?")
        assert out == "SELECT '真的吗?' WHERE a = %s"

    def test_datetime_now_is_translated(self, pg):
        from core.db import _translate

        assert "datetime('now')" not in _translate(
            "INSERT INTO t (a) VALUES (datetime('now'))"
        )


class TestSchema:
    def test_all_tables_created(self, pg):
        from server.auth import _get_conn

        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s", (pg,)
            ).fetchall()
        names = {r["tablename"] for r in rows}
        assert {
            "users",
            "tokens",
            "refresh_tokens",
            "consents",
            "safety_events",
            "subject_requests",
            "user_activity",
            "tasks",
        } <= names


class TestAuthFlow:
    def test_register_login_and_validate(self, pg):
        from server.auth import (
            login_user_with_refresh,
            register_user,
            validate_token,
        )

        assert register_user("pguser", "password123") is None
        session = login_user_with_refresh("pguser", "password123")
        assert session and session["token"]
        assert validate_token(session["token"]) is not None

    def test_duplicate_username_rejected(self, pg):
        from server.auth import register_user

        assert register_user("dup", "password123") is None
        assert register_user("dup", "password123") == "用户名已存在"

    def test_refresh_rotation_works_on_pg(self, pg):
        from server.auth import (
            login_user_with_refresh,
            refresh_session,
            register_user,
        )

        register_user("rot", "password123")
        first = login_user_with_refresh("rot", "password123")
        second = refresh_session(first["refresh_token"])
        assert second and second["token"] != first["token"]
        # 重放旧的 refresh 必须吊销整条会话
        assert refresh_session(first["refresh_token"]) is None

    def test_role_grant(self, pg):
        from server.auth import get_user_role, register_user, set_user_role

        register_user("adm", "password123")
        assert set_user_role("adm", "admin") is True
        from server.auth import _get_conn

        with _get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE username = ?", ("adm",)
            ).fetchone()
        assert get_user_role(int(row["id"])) == "admin"


class TestReturningId:
    def test_insert_returning_id_works_without_lastrowid(self, pg):
        """PG 没有 lastrowid，插入取主键必须走 RETURNING。"""
        from server.auth import register_user
        from server.consent_store import create_subject_request
        from server.safety_store import record_safety_event

        register_user("rid", "password123")
        from server.auth import _get_conn

        with _get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE username = ?", ("rid",)
            ).fetchone()
        user_id = int(row["id"])

        event_id = record_safety_event(user_id, "crisis", "high", "interrupted")
        assert isinstance(event_id, int) and event_id > 0

        req_id = create_subject_request("subject_complaint", "a@b.com")
        assert isinstance(req_id, int) and req_id > 0


class TestComplianceTablesOnPg:
    def test_consent_roundtrip(self, pg):
        from server.auth import _get_conn, register_user
        from server.consent_store import has_consented, list_consents, record_consent

        register_user("cons", "password123")
        with _get_conn() as conn:
            user_id = int(
                conn.execute(
                    "SELECT id FROM users WHERE username = ?", ("cons",)
                ).fetchone()["id"]
            )
        record_consent(user_id, "third_party_data", "v1", ip="1.2.3.4")
        assert has_consented(user_id, "third_party_data", "v1") is True
        assert has_consented(user_id, "third_party_data", "v2") is False
        assert list_consents(user_id)[0]["ip"] == "1.2.3.4"

    def test_usage_guard_roundtrip(self, pg):
        from server.auth import _get_conn, register_user
        from server.usage_guard import status, touch

        register_user("usage", "password123")
        with _get_conn() as conn:
            user_id = int(
                conn.execute(
                    "SELECT id FROM users WHERE username = ?", ("usage",)
                ).fetchone()["id"]
            )
        touch(user_id)
        touch(user_id)
        assert status(user_id)["active_seconds"] > 0

    def test_task_lifecycle_on_pg(self, pg):
        import time

        from core import tasks
        from server.auth import _get_conn, register_user

        register_user("tsk", "password123")
        with _get_conn() as conn:
            user_id = int(
                conn.execute(
                    "SELECT id FROM users WHERE username = ?", ("tsk",)
                ).fetchone()["id"]
            )

        @tasks.register("pg_task")
        def handler(progress):
            progress.update(50, "半程")
            return {"ok": True}

        task_id = tasks.enqueue("pg_task", user_id, {})
        deadline = time.time() + 5
        task = tasks.get_task(task_id)
        while time.time() < deadline and task["status"] not in ("succeeded", "failed"):
            time.sleep(0.05)
            task = tasks.get_task(task_id)
        assert task["status"] == "succeeded", task.get("error")
        tasks._handlers.pop("pg_task", None)


class TestPgVectorStore:
    """pgvector 后端与 ChromaDB 版同签名，调用方不用区分（FR-033）。

    换掉 ChromaDB 的理由不是性能，是本地盘：Chroma 的 persist 目录在每个
    副本各自的磁盘上，多副本下共享不了。
    """

    @pytest.fixture
    def store(self, pg, monkeypatch):
        monkeypatch.setattr("config.VECTOR_BACKEND", "pgvector")
        from memory.pgvector_store import PgVectorStore

        return PgVectorStore(persist_dir="", collection_name="ex_test_memories")

    @staticmethod
    def _embedder(dim=1024):
        from unittest.mock import MagicMock

        emb = MagicMock()

        def _vec(seed):
            v = [0.0] * dim
            v[seed % dim] = 1.0
            return v

        emb.embed.side_effect = lambda docs: [_vec(i) for i in range(len(docs))]
        emb.embed_one.side_effect = lambda q: _vec(0)
        return emb

    def _chunks(self, n=3, speaker="target"):
        return [
            {
                "id": f"c{i}",
                "text_for_embedding": f"文本{i}",
                "display_text": f"原话{i}",
                "metadata": {"dominant_speaker": speaker, "source": "wechat"},
            }
            for i in range(n)
        ]

    def test_ingest_and_count(self, store):
        store.ingest(self._chunks(3), self._embedder())
        assert store.count() == 3

    def test_ingest_is_idempotent_on_same_id(self, store):
        store.ingest(self._chunks(2), self._embedder())
        store.ingest(self._chunks(2), self._embedder())
        assert store.count() == 2, "重复入库应覆盖而非重复插入"

    def test_search_returns_display_text_and_score(self, store):
        store.ingest(self._chunks(3), self._embedder())
        results = store.search("查询", self._embedder(), top_k=2)
        assert len(results) == 2
        assert results[0]["display_text"].startswith("原话")
        # score 口径与 ChromaDB 版一致：1 - 余弦距离
        assert 0.0 <= results[0]["score"] <= 1.0

    def test_search_target_only_filters_by_speaker(self, store):
        store.ingest(self._chunks(2, speaker="target"), self._embedder())
        others = self._chunks(2, speaker="user")
        for i, c in enumerate(others):
            c["id"] = f"o{i}"
        store.ingest(others, self._embedder())

        results = store.search_target_only("查询", self._embedder(), top_k=10)
        assert len(results) == 2
        assert all(r["metadata"]["dominant_speaker"] == "target" for r in results)

    def test_collections_are_isolated(self, pg, monkeypatch):
        """不同镜像的向量不能互相检索到。"""
        monkeypatch.setattr("config.VECTOR_BACKEND", "pgvector")
        from memory.pgvector_store import PgVectorStore

        a = PgVectorStore(persist_dir="", collection_name="ex_a")
        b = PgVectorStore(persist_dir="", collection_name="ex_b")
        a.ingest(self._chunks(2), self._embedder())
        assert a.count() == 2
        assert b.count() == 0
        assert b.search("查询", self._embedder()) == []

    def test_delete_collection_removes_only_own_rows(self, pg, monkeypatch):
        monkeypatch.setattr("config.VECTOR_BACKEND", "pgvector")
        from memory.pgvector_store import PgVectorStore

        a = PgVectorStore(persist_dir="", collection_name="ex_a")
        b = PgVectorStore(persist_dir="", collection_name="ex_b")
        a.ingest(self._chunks(2), self._embedder())
        other = self._chunks(1)
        other[0]["id"] = "keep"
        b.ingest(other, self._embedder())

        a.delete_collection()
        assert a.count() == 0
        assert b.count() == 1

    def test_session_summary_is_stored(self, store):
        store.add_session_summary("这次聊了很久", "slug1", self._embedder())
        assert store.count() == 1
        results = store.search("查询", self._embedder(), top_k=1)
        assert results[0]["metadata"]["source"] == "session_summary"

    def test_factory_picks_pgvector_when_configured(self, pg, monkeypatch):
        monkeypatch.setattr("config.VECTOR_BACKEND", "pgvector")
        from core.factory import build_vector_store
        from memory.pgvector_store import PgVectorStore

        assert isinstance(build_vector_store("anything"), PgVectorStore)

    def test_factory_defaults_to_chroma(self, monkeypatch, tmp_path):
        monkeypatch.setattr("config.VECTOR_BACKEND", "chroma")
        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        from core.factory import build_vector_store
        from memory.vector_store import VectorStore

        assert isinstance(build_vector_store("anything"), VectorStore)
