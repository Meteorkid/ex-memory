"""异步任务：提交、执行、重试、僵尸回收（FR-036 / FR-037）。"""

import hashlib
import json
import os
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import server.auth as auth
    import server.routes as routes_mod
    from core import tasks

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()
    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    noop = MagicMock()
    noop.check = MagicMock()
    monkeypatch.setattr(routes_mod, "_login_limiter", noop)

    tasks.reset_for_tests()
    saved = dict(tasks._handlers)
    yield tmp_path
    tasks._handlers.clear()
    tasks._handlers.update(saved)
    tasks.reset_for_tests()


@pytest.fixture
def client(env):
    from server.app import create_app

    return TestClient(create_app())


def _account(client, request, suffix=""):
    name = "task_" + hashlib.md5((request.node.name + suffix).encode()).hexdigest()[:12]
    client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    resp = client.post(
        "/api/auth/login", json={"username": name, "password": "test123456"}
    )
    token = resp.json()["token"]
    from server.auth import validate_token

    return {"Authorization": f"Bearer {token}"}, validate_token(token)


def _await(task_id, timeout=5):
    from core.tasks import get_task

    deadline = time.time() + timeout
    task = get_task(task_id)
    while time.time() < deadline and task["status"] not in ("succeeded", "failed"):
        time.sleep(0.02)
        task = get_task(task_id)
    return task


class TestLifecycle:
    def test_succeeds_and_records_result(self, env):
        from core import tasks

        @tasks.register("t_ok")
        def handler(progress, *, value):
            progress.update(50, "半程")
            return {"doubled": value * 2}

        task_id = tasks.enqueue("t_ok", 1, {"value": 21})
        task = _await(task_id)
        assert task["status"] == "succeeded"
        assert json.loads(task["result"])["doubled"] == 42
        assert task["progress"] == 100

    def test_failure_is_persisted_not_just_logged(self, env):
        from core import tasks

        @tasks.register("t_boom")
        def handler(progress):
            raise RuntimeError("处理失败了")

        task = _await(tasks.enqueue("t_boom", 1, {}))
        assert task["status"] == "failed"
        assert "处理失败了" in task["error"]
        assert task["finished_at"]

    def test_progress_updates_are_visible(self, env):
        from core import tasks

        gate = threading.Event()

        @tasks.register("t_slow")
        def handler(progress):
            progress.update(30, "进行中")
            gate.wait(2)
            return {}

        task_id = tasks.enqueue("t_slow", 1, {})
        deadline = time.time() + 2
        seen = 0
        while time.time() < deadline and seen < 30:
            seen = tasks.get_task(task_id)["progress"]
            time.sleep(0.02)
        assert seen == 30
        gate.set()
        _await(task_id)

    def test_unregistered_type_is_rejected_at_enqueue(self, env):
        from core import tasks

        with pytest.raises(ValueError):
            tasks.enqueue("不存在的类型", 1, {})


class TestOwnership:
    def test_other_users_task_is_not_visible(self, env):
        from core import tasks

        @tasks.register("t_own")
        def handler(progress):
            return {}

        task_id = tasks.enqueue("t_own", 1, {})
        _await(task_id)
        assert tasks.get_task(task_id, user_id=1) is not None
        assert tasks.get_task(task_id, user_id=2) is None

    def test_api_returns_404_for_other_users_task(self, client, request):
        from core import tasks

        @tasks.register("t_api")
        def handler(progress):
            return {}

        headers_a, user_a = _account(client, request, "a")
        headers_b, _user_b = _account(client, request, "b")
        task_id = tasks.enqueue("t_api", user_a, {})
        _await(task_id)

        assert client.get(f"/api/tasks/{task_id}", headers=headers_a).status_code == 200
        # 不区分「不存在」与「不属于你」，避免任务 ID 存在性泄漏
        assert client.get(f"/api/tasks/{task_id}", headers=headers_b).status_code == 404


class TestRetry:
    def test_only_failed_tasks_can_be_retried(self, env):
        from core import tasks

        calls = []

        @tasks.register("t_retry")
        def handler(progress):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("第一次失败")
            return {"ok": True}

        task_id = tasks.enqueue("t_retry", 7, {})
        assert _await(task_id)["status"] == "failed"

        assert tasks.retry(task_id, user_id=7) is True
        task = _await(task_id)
        assert task["status"] == "succeeded"
        assert task["attempts"] == 2

        # 成功态不可再重试，免得把已完成的任务重跑一遍
        assert tasks.retry(task_id, user_id=7) is False

    def test_retry_checks_ownership(self, env):
        from core import tasks

        @tasks.register("t_own_retry")
        def handler(progress):
            raise RuntimeError("失败")

        task_id = tasks.enqueue("t_own_retry", 7, {})
        _await(task_id)
        assert tasks.retry(task_id, user_id=8) is False


class TestStaleReclaim:
    def test_zombie_running_tasks_are_reclaimed(self, env):
        """进程崩溃会留下 running 却无人执行的任务。"""
        from core import tasks
        from server.auth import _get_conn

        @tasks.register("t_zombie")
        def handler(progress):
            return {}

        task_id = tasks.enqueue("t_zombie", 1, {})
        _await(task_id)
        with _get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='running', heartbeat_at='2020-01-01T00:00:00+00:00'"
                " WHERE id = ?",
                (task_id,),
            )
            conn.commit()

        assert tasks.reclaim_stale() == 1
        task = tasks.get_task(task_id)
        assert task["status"] == "failed"
        assert "重启" in task["error"]

    def test_fresh_running_tasks_are_left_alone(self, env):
        from core import tasks
        from server.auth import _get_conn

        @tasks.register("t_fresh")
        def handler(progress):
            return {}

        task_id = tasks.enqueue("t_fresh", 1, {})
        _await(task_id)
        with _get_conn() as conn:
            conn.execute(
                "UPDATE tasks SET status='running', heartbeat_at=? WHERE id = ?",
                (tasks._now(), task_id),
            )
            conn.commit()
        assert tasks.reclaim_stale() == 0


class TestApiSurface:
    def test_task_view_hides_payload(self, client, request):
        """payload 里可能有暂存文件路径，不该对外暴露。"""
        from core import tasks

        @tasks.register("t_view")
        def handler(progress, *, secret_path):
            return {"ok": True}

        headers, user_id = _account(client, request)
        task_id = tasks.enqueue("t_view", user_id, {"secret_path": "/tmp/secret"})
        _await(task_id)

        body = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert "payload" not in body
        assert "/tmp/secret" not in json.dumps(body)
        assert body["status"] == "succeeded"

    def test_list_only_returns_own_tasks(self, client, request):
        from core import tasks

        @tasks.register("t_list")
        def handler(progress):
            return {}

        headers_a, user_a = _account(client, request, "a")
        _headers_b, user_b = _account(client, request, "b")
        _await(tasks.enqueue("t_list", user_a, {}))
        _await(tasks.enqueue("t_list", user_b, {}))

        tasks_a = client.get("/api/tasks", headers=headers_a).json()["tasks"]
        assert len(tasks_a) == 1

    def test_retry_endpoint_rejects_non_failed(self, client, request):
        from core import tasks

        @tasks.register("t_retry_api")
        def handler(progress):
            return {}

        headers, user_id = _account(client, request)
        task_id = tasks.enqueue("t_retry_api", user_id, {})
        _await(task_id)
        resp = client.post(f"/api/tasks/{task_id}/retry", headers=headers)
        assert resp.status_code == 400
