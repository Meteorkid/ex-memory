"""API 路由冒烟测试。"""

import json
import os
import pytest
from pathlib import Path
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
