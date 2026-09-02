"""三副本一致性（NFR-030）：M1 全部改造的验收点。

用三个独立的 app 实例模拟三个副本，共享同一份 Postgres 与 Redis。
改造前这些用例全都会失败——限流额度按副本数翻倍、登录换个副本就不认、
用户纠正只在一个副本生效。

需要本机同时有 Postgres 与 Redis，否则整组跳过。
"""

import os
import time
import uuid

import pytest
from fastapi.testclient import TestClient

PG_URL = os.getenv("TEST_DATABASE_URL", "postgresql:///ex_memory_dev")
REDIS_URL = "redis://localhost:6379/15"


def _infra_available() -> bool:
    try:
        import psycopg

        from core import kv

        with psycopg.connect(PG_URL, connect_timeout=2):
            pass
        kv.RedisBackend(REDIS_URL)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _infra_available(), reason="需要本机 PostgreSQL + Redis"
)


@pytest.fixture
def replicas(tmp_path, monkeypatch):
    """起三个共享后端的 app 实例。"""
    import psycopg

    from core import db, kv

    schema = f"r_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")

    monkeypatch.setattr(
        "config.DATABASE_URL", f"{PG_URL}?options=-csearch_path%3D{schema},public"
    )
    monkeypatch.setattr("config.REDIS_URL", REDIS_URL)
    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    db.reset_pool()
    kv.reset_for_tests()
    kv.configure(REDIS_URL)
    # 限流计数存在共享 Redis 里，跨用例累积会让后续登录被 429 拦掉，
    # 且失败原因极难定位。DB 15 是测试专用库，每个用例开始前清空。
    kv.RedisBackend(REDIS_URL).raw().flushdb()

    from server.app import create_app

    clients = [TestClient(create_app()) for _ in range(3)]
    yield clients

    for c in clients:
        c.close()
    db.reset_pool()
    kv.reset_for_tests()
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class TestIdentityAcrossReplicas:
    def test_login_on_one_replica_works_on_others(self, replicas):
        """会话不能绑在某个副本上。"""
        a, b, c = replicas
        name = _unique("mr")
        a.post("/api/auth/register", json={"username": name, "password": "test123456"})
        token = a.post(
            "/api/auth/login", json={"username": name, "password": "test123456"}
        ).json()["token"]

        headers = {"Authorization": f"Bearer {token}"}
        assert b.get("/api/exes", headers=headers).status_code == 200
        assert c.get("/api/exes", headers=headers).status_code == 200

    def test_revoke_on_one_replica_takes_effect_everywhere(self, replicas):
        a, b, _c = replicas
        name = _unique("mr")
        a.post("/api/auth/register", json={"username": name, "password": "test123456"})
        session = a.post(
            "/api/auth/login", json={"username": name, "password": "test123456"}
        ).json()
        headers = {"Authorization": f"Bearer {session['token']}"}

        assert a.post("/api/auth/revoke-all", headers=headers).status_code == 200
        assert b.get("/api/exes", headers=headers).status_code == 401


class TestSharedRateLimit:
    def test_login_limit_is_not_multiplied_by_replica_count(
        self, replicas, monkeypatch
    ):
        """🔴 按用户名限流若只在单副本内生效，换个连接落到另一副本就绕过去了。"""
        import server.routes as routes_mod
        from server.middleware import LoginRateLimiter

        # 用真实限流器（其余用例里被打桩掉了）
        for module in (routes_mod,):
            monkeypatch.setattr(module, "_login_limiter", LoginRateLimiter())

        name = _unique("rl")
        replicas[0].post(
            "/api/auth/register", json={"username": name, "password": "test123456"}
        )

        statuses = []
        # 轮流打到三个副本，总数超过单副本额度（5 次/分钟/用户名）
        for i in range(8):
            client = replicas[i % 3]
            statuses.append(
                client.post(
                    "/api/auth/login",
                    json={"username": name, "password": "wrong-password"},
                ).status_code
            )
        assert 429 in statuses, "限流额度被副本数放大了"


class TestSharedUsageState:
    def test_usage_accumulates_across_replicas(self, replicas):
        """用量统计不能各副本各算各的。"""
        a, b, _c = replicas
        name = _unique("ug")
        a.post("/api/auth/register", json={"username": name, "password": "test123456"})
        token = a.post(
            "/api/auth/login", json={"username": name, "password": "test123456"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        from server.auth import validate_token
        from server.usage_guard import touch

        user_id = validate_token(token)
        touch(user_id)
        time.sleep(0.05)
        touch(user_id)

        # 任一副本读到的都是同一份累计值
        seen = {
            client.get("/api/user/health/stats", headers=headers).json()[
                "active_seconds"
            ]
            for client in (a, b)
        }
        assert len(seen) == 1 and seen.pop() > 0


class TestCacheInvalidationBroadcast:
    def test_invalidation_reaches_all_replicas(self, replicas):
        """D-12：用户纠正「ta 不会这样」不能只在一个副本生效。"""
        import server.routes as routes

        from core import kv

        # 三个副本共用同一个进程内缓存对象（同一进程），
        # 这里验证的是广播链路本身通到了本地处理器
        routes._engine_cache[(1, "shared-slug")] = object()
        routes._engine_cache[(2, "shared-slug")] = object()
        routes._engine_cache[(1, "other")] = object()

        kv.broadcast_invalidate("shared-slug")

        deadline = time.time() + 3
        while time.time() < deadline and any(
            k[1] == "shared-slug" for k in routes._engine_cache.keys()
        ):
            time.sleep(0.05)

        assert not any(k[1] == "shared-slug" for k in routes._engine_cache.keys())
        assert (1, "other") in routes._engine_cache
        routes._engine_cache.clear()


class TestTasksAcrossReplicas:
    def test_task_submitted_on_one_replica_is_visible_on_another(self, replicas):
        """任务状态落库而非放内存，换副本查得到。"""
        from core import tasks

        a, b, _c = replicas
        name = _unique("tk")
        a.post("/api/auth/register", json={"username": name, "password": "test123456"})
        token = a.post(
            "/api/auth/login", json={"username": name, "password": "test123456"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        from server.auth import validate_token

        user_id = validate_token(token)

        @tasks.register("mr_task")
        def handler(progress):
            return {"ok": True}

        try:
            task_id = tasks.enqueue("mr_task", user_id, {})
            deadline = time.time() + 5
            task = tasks.get_task(task_id)
            while time.time() < deadline and task["status"] not in (
                "succeeded",
                "failed",
            ):
                time.sleep(0.05)
                task = tasks.get_task(task_id)

            resp = b.get(f"/api/tasks/{task_id}", headers=headers)
            assert resp.status_code == 200
            assert resp.json()["status"] == "succeeded"
        finally:
            tasks._handlers.pop("mr_task", None)


class TestVectorsAcrossReplicas:
    def test_pgvector_is_visible_from_every_replica(self, replicas, monkeypatch):
        """Chroma 的本地 persist 目录在多副本下共享不了，这是换 pgvector 的理由。"""
        from unittest.mock import MagicMock

        monkeypatch.setattr("config.VECTOR_BACKEND", "pgvector")
        from core.factory import build_vector_store

        def _emb():
            e = MagicMock()
            e.embed.side_effect = lambda docs: [[0.0] * 1023 + [1.0] for _ in docs]
            e.embed_one.side_effect = lambda q: [0.0] * 1023 + [1.0]
            return e

        writer = build_vector_store("shared_exe")
        writer.ingest(
            [
                {
                    "id": "x1",
                    "text_for_embedding": "ta 说过的话",
                    "display_text": "原话",
                    "metadata": {"dominant_speaker": "target"},
                }
            ],
            _emb(),
        )

        # 另一个副本构造自己的 store 实例，应当读到同一份数据
        reader = build_vector_store("shared_exe")
        assert reader.count() == 1
        assert reader.search_target_only("查询", _emb())[0]["display_text"] == "原话"
