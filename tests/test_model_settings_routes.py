"""模型配置 API 不越权、不回显秘密，所有路由响应禁止缓存。"""

import base64
import asyncio
import json
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import config
    from server import auth
    import server.account_lifecycle as lifecycle
    import server.routes as routes

    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "DB_DIR", tmp_path)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "METEOR_STORE_SSO_ENABLED", False)
    monkeypatch.setattr(
        config,
        "MODEL_SETTINGS_ENCRYPTION_KEY",
        base64.b64encode(b"t" * 32).decode(),
        raising=False,
    )
    monkeypatch.setattr(
        lifecycle, "_feedback_path", lambda: tmp_path / "feedback.jsonl"
    )
    monkeypatch.setattr(
        lifecycle, "_sticker_dir", lambda user_id: tmp_path / f"stickers-{user_id}"
    )
    auth.init_db()
    headers = []
    for username in ("route_alice", "route_bob"):
        auth.register_user(username, "test-password")
        headers.append(
            {"Authorization": "Bearer " + auth.login_user(username, "test-password")}
        )
    from server.app import create_app

    routes._engine_cache.clear()
    with TestClient(create_app()) as client:
        yield client, headers, tmp_path
    routes._engine_cache.clear()


_PATH = "/api/account/model-settings"
_SECRET = "sk-user-synthetic-secret-4321"


def test_authentication_and_owner_isolation(setup):
    client, headers, _ = setup
    anonymous = client.get(_PATH)
    assert anonymous.status_code == 401
    assert anonymous.headers["Cache-Control"] == "no-store"
    saved = client.put(
        _PATH,
        headers=headers[0],
        json={"expected_revision": 0, "llm": {"api_key": _SECRET}},
    )
    assert saved.status_code == 200
    assert saved.headers["Cache-Control"] == "no-store"
    assert _SECRET not in saved.text
    own = client.get(_PATH, headers=headers[0]).json()
    other = client.get(_PATH, headers=headers[1]).json()
    assert own["llm"]["has_key"] is True
    assert other["llm"]["has_key"] is False
    assert client.get(_PATH + "?user_id=1", headers=headers[1]).status_code == 400


def test_outer_rate_limit_and_import_status_are_not_cached(setup, monkeypatch):
    client, headers, _ = setup
    anonymous = client.get("/api/import/config")
    assert anonymous.status_code == 401
    assert anonymous.headers["Cache-Control"] == "no-store"
    own = client.get("/api/import/config", headers=headers[0])
    assert own.status_code == 200
    assert own.headers["Cache-Control"] == "no-store"
    from core import kv

    monkeypatch.setattr(kv, "incr_window", lambda *args, **kwargs: 9999)
    limited = client.put(
        _PATH,
        headers=headers[0],
        json={"expected_revision": 0, "llm": {"api_key": _SECRET}},
    )
    assert limited.status_code == 429
    assert limited.headers["Cache-Control"] == "no-store"
    assert _SECRET not in limited.text


@pytest.mark.parametrize(
    "payload",
    [
        {"expected_revision": 0, "user_id": 2, "llm": {"api_key": _SECRET}},
        {"expected_revision": 0, "llm": {"api_key": {"secret": _SECRET}}},
        {"expected_revision": 0, "llm": {"api_key": _SECRET, "delete_key": "yes"}},
        {
            "expected_revision": 0,
            "llm": {"api_key": _SECRET, "base_url": "http://127.0.0.1"},
        },
        {"expected_revision": _SECRET},
        {"expected_revision": 0, _SECRET: "invalid-extra-field"},
        {"expected_revision": 0, "llm": {"api_key": "short"}},
        {"expected_revision": 0, "llm": {"api_key": _SECRET, "provider": "custom"}},
    ],
)
def test_invalid_request_never_echoes_input(setup, payload):
    client, headers, _ = setup
    response = client.put(_PATH, headers=headers[0], json=payload)
    assert response.status_code in (400, 422)
    assert _SECRET not in response.text
    assert "input" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_json_size_and_revision_errors_are_private(setup):
    client, headers, _ = setup
    malformed = client.put(_PATH, headers=headers[0], content=b'{"api_key": "secret"')
    assert malformed.status_code == 400
    assert "secret" not in malformed.text
    oversized = client.put(_PATH, headers=headers[0], content="x" * 17000)
    assert oversized.status_code == 413
    conflict = client.put(_PATH, headers=headers[0], json={"expected_revision": 4})
    assert conflict.status_code == 409
    for response in (malformed, oversized, conflict):
        assert response.headers["Cache-Control"] == "no-store"


def test_body_stream_stops_at_limit_without_reading_remaining_data():
    from fastapi import HTTPException, Request
    from server.model_settings_routes import save_model_settings

    received = []

    async def receive():
        received.append(True)
        assert len(received) <= 3, "超限后不得继续读取请求体"
        return {
            "type": "http.request",
            "more_body": True,
            "body": b"x" * (8192 if len(received) < 3 else 1),
        }

    request = Request({"type": "http", "query_string": b""}, receive=receive)
    with pytest.raises(HTTPException) as error:
        asyncio.run(save_model_settings(request, user_id=1))
    assert error.value.status_code == 413
    assert len(received) == 3


def test_save_evicts_only_own_cached_engines(setup):
    import server.routes as routes
    from config import _account_llm_clients
    from core.llm_router import _account_routers

    client, headers, _ = setup
    routes._engine_cache[(1, "same", 0)] = object()
    routes._engine_cache[(2, "same", 0)] = object()
    _account_llm_clients[(1, 0)] = object()
    _account_routers[(1, 0)] = object()
    response = client.put(
        _PATH,
        headers=headers[0],
        json={"expected_revision": 0, "llm": {"api_key": _SECRET}},
    )
    assert response.status_code == 200
    assert not any(key[0] == 1 for key in routes._engine_cache)
    assert any(key[0] == 2 for key in routes._engine_cache)
    assert not any(key[0] == 1 for key in _account_llm_clients)
    assert not any(key[0] == 1 for key in _account_routers)


def test_secret_model_repr_is_redacted():
    from server.model_settings_routes import SettingsUpdate

    value = SettingsUpdate.model_validate(
        {"expected_revision": 0, "llm": {"api_key": _SECRET}}
    )
    assert _SECRET not in repr(value)


def test_unexpected_error_is_redacted_and_private(setup, monkeypatch, caplog):
    client, headers, _ = setup

    def fail(*args, **kwargs):
        raise RuntimeError(_SECRET)

    monkeypatch.setattr("server.model_settings_routes.update_model_settings", fail)
    response = client.put(_PATH, headers=headers[0], json={"expected_revision": 0})
    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"
    assert _SECRET not in response.text
    assert _SECRET not in caplog.text


def test_export_excludes_secrets_and_deletion_removes_credentials(setup):
    client, headers, _ = setup
    from server.account_lifecycle import delete_account, export_account, verify_deletion
    from server.auth import _get_conn

    client.put(
        _PATH,
        headers=headers[0],
        json={"expected_revision": 0, "llm": {"api_key": _SECRET}},
    )
    with _get_conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM refresh_tokens WHERE user_id = 1"
            ).fetchone()["n"]
            > 0
        )
    path = export_account(1)
    try:
        with zipfile.ZipFile(path) as archive:
            account_text = archive.read("account.json").decode()
            public = json.loads(account_text)["model_settings"]
            assert public["llm"]["key_last4"] == "4321"
            assert _SECRET not in account_text
            assert "ciphertext" not in account_text
    finally:
        path.unlink()
    delete_account(1)
    assert verify_deletion(1) == []
    with _get_conn() as conn:
        for table in ("user_model_settings", "tokens", "refresh_tokens"):
            assert (
                conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = 1"
                ).fetchone()["n"]
                == 0
            )


def test_missing_master_key_reports_state_and_rejects_new_secret(setup, monkeypatch):
    client, headers, _ = setup
    monkeypatch.setattr("config.MODEL_SETTINGS_ENCRYPTION_KEY", "")
    assert client.get(_PATH, headers=headers[0]).json()["storage_available"] is False
    response = client.put(
        _PATH,
        headers=headers[0],
        json={"expected_revision": 0, "llm": {"api_key": _SECRET}},
    )
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert _SECRET not in response.text
