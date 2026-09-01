"""刷新令牌与会话吊销（FR-040）。

原先只有一种固定 7 天的 token：被盗后的可利用窗口就是它的全部生命周期，
且没有任何主动吊销手段。
"""

import hashlib
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import server.auth as auth
    import server.routes as routes_mod

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()
    noop = MagicMock()
    noop.check = MagicMock()
    monkeypatch.setattr(routes_mod, "_login_limiter", noop)

    from server.app import create_app

    return TestClient(create_app())


def _login(client, request, suffix=""):
    name = "sess_" + hashlib.md5((request.node.name + suffix).encode()).hexdigest()[:12]
    client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    resp = client.post(
        "/api/auth/login", json={"username": name, "password": "test123456"}
    )
    return resp.json()


class TestLoginIssuesPair:
    def test_login_returns_access_and_refresh(self, client, request):
        body = _login(client, request)
        assert body["token"] and body["refresh_token"]
        assert body["token"] != body["refresh_token"]
        assert body["expires_in"] > 0

    def test_access_token_works(self, client, request):
        body = _login(client, request)
        headers = {"Authorization": f"Bearer {body['token']}"}
        assert client.get("/api/exes", headers=headers).status_code == 200


class TestRefreshRotation:
    def test_refresh_yields_new_pair(self, client, request):
        body = _login(client, request)
        resp = client.post(
            "/api/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
        assert resp.status_code == 200
        new = resp.json()
        assert new["token"] != body["token"]
        assert new["refresh_token"] != body["refresh_token"]
        assert new["session_id"] == body["session_id"]

    def test_old_access_token_stops_working_after_refresh(self, client, request):
        """一次登录不该留下多把有效钥匙。"""
        body = _login(client, request)
        old_headers = {"Authorization": f"Bearer {body['token']}"}
        client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert client.get("/api/exes", headers=old_headers).status_code == 401

    def test_replayed_refresh_revokes_entire_session(self, client, request):
        """🔴 已轮换的 refresh 再次出现说明可能被盗，吊销整条会话链。"""
        body = _login(client, request)
        first = client.post(
            "/api/auth/refresh", json={"refresh_token": body["refresh_token"]}
        ).json()

        # 重放旧的 refresh
        replay = client.post(
            "/api/auth/refresh", json={"refresh_token": body["refresh_token"]}
        )
        assert replay.status_code == 401

        # 盗用方和真实用户都得重新登录
        assert (
            client.get(
                "/api/exes", headers={"Authorization": f"Bearer {first['token']}"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/auth/refresh", json={"refresh_token": first["refresh_token"]}
            ).status_code
            == 401
        )

    def test_unknown_refresh_is_rejected(self, client):
        assert (
            client.post(
                "/api/auth/refresh", json={"refresh_token": "x" * 40}
            ).status_code
            == 401
        )


class TestRevokeAll:
    def test_logs_out_every_device(self, client, request):
        first = _login(client, request)
        second = client.post(
            "/api/auth/login",
            json={
                "username": "sess_"
                + hashlib.md5(request.node.name.encode()).hexdigest()[:12],
                "password": "test123456",
            },
        ).json()

        resp = client.post(
            "/api/auth/revoke-all",
            headers={"Authorization": f"Bearer {second['token']}"},
        )
        assert resp.status_code == 200

        for session in (first, second):
            assert (
                client.get(
                    "/api/exes", headers={"Authorization": f"Bearer {session['token']}"}
                ).status_code
                == 401
            )
            assert (
                client.post(
                    "/api/auth/refresh",
                    json={"refresh_token": session["refresh_token"]},
                ).status_code
                == 401
            )

    def test_sessions_endpoint_lists_active_ones(self, client, request):
        body = _login(client, request)
        headers = {"Authorization": f"Bearer {body['token']}"}
        sessions = client.get("/api/auth/sessions", headers=headers).json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == body["session_id"]

    def test_revoked_sessions_disappear_from_list(self, client, request):
        body = _login(client, request)
        headers = {"Authorization": f"Bearer {body['token']}"}
        client.post("/api/auth/revoke-all", headers=headers)

        fresh = client.post(
            "/api/auth/login",
            json={
                "username": "sess_"
                + hashlib.md5(request.node.name.encode()).hexdigest()[:12],
                "password": "test123456",
            },
        ).json()
        sessions = client.get(
            "/api/auth/sessions",
            headers={"Authorization": f"Bearer {fresh['token']}"},
        ).json()["sessions"]
        assert len(sessions) == 1


class TestExpiry:
    def test_expired_refresh_is_rejected(self, client, request, monkeypatch):
        import server.auth as auth

        monkeypatch.setattr(auth, "REFRESH_TOKEN_EXPIRY_SECONDS", -1)
        body = _login(client, request)
        assert (
            client.post(
                "/api/auth/refresh", json={"refresh_token": body["refresh_token"]}
            ).status_code
            == 401
        )

    def test_access_token_is_short_lived_by_default(self):
        import server.auth as auth

        assert auth.ACCESS_TOKEN_EXPIRY_SECONDS <= 24 * 3600
        assert auth.REFRESH_TOKEN_EXPIRY_SECONDS > auth.ACCESS_TOKEN_EXPIRY_SECONDS
