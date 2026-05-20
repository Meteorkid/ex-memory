"""API 路由冒烟测试。"""

import json
import asyncio
import time
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "users.db"
    monkeypatch.setattr("server.auth.DB_PATH", test_db)
    monkeypatch.setattr("server.auth.DB_DIR", test_db.parent)
    import server.auth as auth
    with auth._get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS tokens")
        conn.execute("DROP TABLE IF EXISTS users")
        conn.commit()
    auth.init_db()

    exes = tmp_path / "exes"
    exes.mkdir()
    slug = "owned"
    ex_dir = exes / slug
    ex_dir.mkdir()
    (ex_dir / "meta.json").write_text(
        json.dumps({"name": "O", "slug": slug, "owner_user_id": 1, "created_at": "2024-01-01"}),
        encoding="utf-8",
    )
    (ex_dir / "SKILL.md").write_text("# skill", encoding="utf-8")

    monkeypatch.setattr("config.EXES_DIR", exes)
    monkeypatch.setattr("config.get_ex_dir", lambda s: exes / s)
    monkeypatch.setattr("config.SINGLE_USER_MODE", False)
    monkeypatch.setattr("core.exe_access.get_ex_dir", lambda s: exes / s)

    import server.routes as routes
    monkeypatch.setattr(routes, "get_ex_dir", lambda s: exes / s)
    monkeypatch.setattr(routes, "_login_limiter", None)
    routes._engine_cache.clear()

    from server.app import create_app
    return TestClient(create_app())


def _register_and_login(client, username="u1", password="pass1234"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    return r.json()["token"]


def test_stickers_requires_auth(client):
    assert client.get("/api/stickers").status_code == 401


def test_list_exes_owner_filter(client):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/api/exes", headers=headers)
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["slug"] == "owned"


def test_forbidden_other_user_exe(client, tmp_path):
    from config import get_ex_dir
    meta_path = get_ex_dir("owned") / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["owner_user_id"] = 999
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    token = _register_and_login(client, "other", "pass1234")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/api/exes/owned/wallet", headers=headers)
    assert r.status_code == 403


def test_import_data_uses_owned_ex_dir(client, monkeypatch):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    monkeypatch.setattr(
        "config.get_embedding_config",
        lambda: {"api_key": "emb-key", "base_url": "http://example.test", "model": "emb"},
    )

    class FakeEmbedder:
        def __init__(self, *args, **kwargs):
            pass

    class FakeVectorStore:
        def __init__(self, persist_dir, collection_name):
            self.persist_dir = persist_dir
            self.collection_name = collection_name

    def fake_ingest(file_path, slug, target_name, vector_store, embedder):
        assert slug == "owned"
        assert target_name == "O"
        assert vector_store.persist_dir.endswith("owned/chroma_db")
        return [{"content": "hello"}], 1

    monkeypatch.setattr("memory.embedder.Embedder", FakeEmbedder)
    monkeypatch.setattr("memory.vector_store.VectorStore", FakeVectorStore)
    monkeypatch.setattr("memory.ingest.ingest_wechat_file", fake_ingest)

    r = client.post(
        "/api/exes/owned/import",
        headers=headers,
        data={"target_name": "O"},
        files={"file": ("chat.json", b"[]", "application/json")},
    )

    assert r.status_code == 200
    assert "导入完成" in r.json()["message"]


def test_custom_sticker_content_requires_owner_auth(client, tmp_path, monkeypatch):
    import core.sticker_manager as stickers

    monkeypatch.setattr(stickers, "CUSTOM_BASE", tmp_path / "stickers" / "custom")
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    upload = client.post(
        "/api/stickers/upload",
        headers=headers,
        data={"label": "测试", "category": "custom"},
        files={"file": ("x.png", b"fake-png", "image/png")},
    )
    assert upload.status_code == 200
    sticker_id = upload.json()["id"]

    assert client.get(f"/api/stickers/{sticker_id}/content").status_code == 401

    own = client.get(f"/api/stickers/{sticker_id}/content", headers=headers)
    assert own.status_code == 200
    assert own.content == b"fake-png"
    assert own.headers["cache-control"] == "private, no-store"

    other_token = _register_and_login(client, "other", "pass1234")
    other_headers = {"Authorization": f"Bearer {other_token}"}
    other = client.get(f"/api/stickers/{sticker_id}/content", headers=other_headers)
    assert other.status_code == 404


def test_sticker_upload_rejects_large_file_before_storage(client, tmp_path, monkeypatch):
    import core.sticker_manager as stickers

    monkeypatch.setattr(stickers, "CUSTOM_BASE", tmp_path / "stickers" / "custom")
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    oversized = b"x" * (2 * 1024 * 1024 + 1)
    r = client.post(
        "/api/stickers/upload",
        headers=headers,
        data={"label": "big", "category": "custom"},
        files={"file": ("big.png", oversized, "image/png")},
    )

    assert r.status_code == 413
    assert not (tmp_path / "stickers" / "custom" / "u1").exists()


def test_health_and_security_headers(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"

    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"
    assert ready.json()["checks"]["data_dir"] == "ok"


@pytest.mark.asyncio
async def test_iterate_sync_stream_does_not_block_event_loop():
    from server.routes import _iterate_sync_stream

    def slow_stream():
        time.sleep(0.05)
        yield {"type": "text", "content": "ok"}

    async def collect():
        return [item async for item in _iterate_sync_stream(slow_stream)]

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.01)

    assert not task.done()
    assert await task == [{"type": "text", "content": "ok"}]
