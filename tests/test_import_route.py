"""D-04 回归 + NFR-000：import_data 主链路与事件循环不阻塞。"""

import inspect
import json
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    import server.auth as auth

    test_db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", test_db)
    monkeypatch.setattr(auth, "DB_DIR", test_db.parent)
    with auth._get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS tokens")
        conn.execute("DROP TABLE IF EXISTS users")
        conn.commit()
    auth.init_db()

    exes = tmp_path / "exes"
    exes.mkdir()
    monkeypatch.setattr("config.EXES_DIR", exes)
    monkeypatch.setattr("config.SINGLE_USER_MODE", False)

    import server.routes as routes

    monkeypatch.setattr(routes, "_login_limiter", None)
    with routes._engine_cache_lock:
        routes._engine_cache.clear()
    yield exes
    with routes._engine_cache_lock:
        routes._engine_cache.clear()


def _make_exe(exes_dir, slug: str, owner: int = 1):
    ex_dir = exes_dir / slug
    ex_dir.mkdir(parents=True, exist_ok=True)
    (ex_dir / "meta.json").write_text(
        json.dumps({"name": slug, "slug": slug, "owner_user_id": owner}),
        encoding="utf-8",
    )


def _login(client, username="importer", password="pass1234"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    return {"Authorization": f"Bearer {r.json()['token']}"}


_EMB_CFG = {"api_key": "test-key", "base_url": "http://test", "model": "test-model"}


def test_import_route_is_sync():
    """D-04：import_data 必须是同步路由，FastAPI 才会放进线程池执行。"""
    from server.routes import import_data

    assert not inspect.iscoroutinefunction(import_data), (
        "回归：import_data 声明为 async 会在事件循环上同步阻塞"
    )


def _grant_third_party_consent(client, headers):
    """FR-016：导入前必须有第三方数据处理的独立同意。"""
    from config import THIRD_PARTY_DATA_POLICY_VERSION

    resp = client.post(
        "/api/consents",
        json={
            "policy_type": "third_party_data",
            "policy_version": THIRD_PARTY_DATA_POLICY_VERSION,
        },
        headers=headers,
    )
    assert resp.status_code == 200


def test_import_without_third_party_consent_is_rejected(env):
    """FR-016：没有独立同意时导入必须被拒，而不是静默通过。"""
    from server.app import create_app

    _make_exe(env, "imp")
    client = TestClient(create_app())
    headers = _login(client)

    resp = client.post(
        "/api/exes/imp/import",
        files={"file": ("chat.json", b"{}", "application/json")},
        headers=headers,
    )
    assert resp.status_code == 403
    assert "合法处理基础" in resp.json()["detail"]


def test_import_requires_auth(env):
    from server.app import create_app

    client = TestClient(create_app())
    resp = client.post(
        "/api/exes/imp/import",
        files={"file": ("chat.json", b"{}", "application/json")},
    )
    assert resp.status_code == 401


def test_import_success(env):
    """导入主链路：上传 → 解析入库 → 返回统计（原为零覆盖路径）。"""
    from server.app import create_app

    _make_exe(env, "imp")
    client = TestClient(create_app())
    headers = _login(client)
    _grant_third_party_consent(client, headers)

    with (
        patch("config.get_embedding_config", return_value=_EMB_CFG),
        patch("memory.embedder.Embedder"),
        patch("memory.vector_store.VectorStore"),
        patch(
            "memory.ingest.ingest_wechat_file",
            return_value=([{"sender": "ta", "content": "hi"}], 3),
        ) as mock_ingest,
    ):
        resp = client.post(
            "/api/exes/imp/import",
            files={"file": ("chat.json", b"{}", "application/json")},
            data={"target_name": "ta"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert "解析 1 条消息" in resp.json()["message"]
    assert "入库 3 个切片" in resp.json()["message"]
    mock_ingest.assert_called_once()


def test_import_does_not_block_event_loop(env):
    """D-04 行为验证：导入阻塞期间，其他请求不被挂起。

    使用 with TestClient 共享同一个事件循环（等价于单个 uvicorn worker）：
    若 import_data 是 async 且内部同步阻塞，/health 会等导入结束才返回。
    """
    from server.app import create_app

    _make_exe(env, "imp")
    app = create_app()

    def slow_ingest(file_path, slug, target_name, vector_store, embedder):
        time.sleep(1.5)
        return [{"sender": "ta", "content": "hi"}], 1

    with (
        patch("config.get_embedding_config", return_value=_EMB_CFG),
        patch("memory.embedder.Embedder"),
        patch("memory.vector_store.VectorStore"),
        patch("memory.ingest.ingest_wechat_file", side_effect=slow_ingest),
    ):
        with TestClient(app) as client:
            headers = _login(client)
            _grant_third_party_consent(client, headers)
            result = {}

            def do_import():
                result["resp"] = client.post(
                    "/api/exes/imp/import",
                    files={"file": ("chat.json", b"{}", "application/json")},
                    data={"target_name": "ta"},
                    headers=headers,
                )

            t = threading.Thread(target=do_import)
            t.start()
            time.sleep(0.5)  # 等导入进入阻塞段

            start = time.perf_counter()
            health = client.get("/health")
            elapsed = time.perf_counter() - start
            t.join()

            assert health.status_code == 200
            assert result["resp"].status_code == 200
            assert elapsed < 0.7, (
                f"回归：导入阻塞了事件循环，/health 延迟 {elapsed:.2f}s"
            )
