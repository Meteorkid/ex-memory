"""复核队列的管理员权限（补 M0 遗留：接成 API 时必须是管理员专属）。

队列里是用户最脆弱时刻的记录与被模拟者的投诉，任何登录用户都能翻看
是不可接受的。
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


def _account(client, name, admin=False):
    client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    if admin:
        from server.auth import set_user_role

        assert set_user_role(name, "admin") is True
    resp = client.post(
        "/api/auth/login", json={"username": name, "password": "test123456"}
    )
    token = resp.json()["token"]
    from server.auth import validate_token

    return {"Authorization": f"Bearer {token}"}, validate_token(token)


def _seed_event(user_id):
    from server.safety_store import record_safety_event

    return record_safety_event(
        user_id=user_id,
        event_type="crisis",
        severity="high",
        action_taken="interrupted",
        raw_text="我不想活了",
    )


class TestAccessControl:
    def test_anonymous_is_rejected(self, client):
        assert client.get("/api/admin/safety/reviews").status_code == 401

    def test_ordinary_user_is_rejected(self, client, request):
        headers, user_id = _account(
            client, "plain_" + hashlib.md5(request.node.name.encode()).hexdigest()[:8]
        )
        _seed_event(user_id)
        resp = client.get("/api/admin/safety/reviews", headers=headers)
        assert resp.status_code == 403
        assert "管理员" in resp.json()["detail"]

    def test_ordinary_user_cannot_resolve(self, client, request):
        headers, user_id = _account(
            client, "plain2_" + hashlib.md5(request.node.name.encode()).hexdigest()[:8]
        )
        event_id = _seed_event(user_id)
        resp = client.post(
            f"/api/admin/safety/reviews/{event_id}",
            json={"status": "resolved"},
            headers=headers,
        )
        assert resp.status_code == 403

    def test_subject_request_queue_is_admin_only(self, client, request):
        headers, _ = _account(
            client, "plain3_" + hashlib.md5(request.node.name.encode()).hexdigest()[:8]
        )
        assert (
            client.get("/api/admin/subject-requests", headers=headers).status_code
            == 403
        )


class TestAdminWorkflow:
    def test_admin_sees_and_resolves_safety_events(self, client, request):
        suffix = hashlib.md5(request.node.name.encode()).hexdigest()[:8]
        admin_headers, _ = _account(client, "adm_" + suffix, admin=True)
        _user_headers, user_id = _account(client, "usr_" + suffix)
        event_id = _seed_event(user_id)

        events = client.get("/api/admin/safety/reviews", headers=admin_headers).json()[
            "events"
        ]
        assert [e["id"] for e in events] == [event_id]

        resp = client.post(
            f"/api/admin/safety/reviews/{event_id}",
            json={"status": "resolved", "note": "已联系用户"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert (
            client.get("/api/admin/safety/reviews", headers=admin_headers).json()[
                "events"
            ]
            == []
        )

    def test_invalid_status_rejected(self, client, request):
        suffix = hashlib.md5(request.node.name.encode()).hexdigest()[:8]
        admin_headers, admin_id = _account(client, "adm2_" + suffix, admin=True)
        event_id = _seed_event(admin_id)
        resp = client.post(
            f"/api/admin/safety/reviews/{event_id}",
            json={"status": "随便写的"},
            headers=admin_headers,
        )
        assert resp.status_code == 400

    def test_admin_handles_subject_request(self, client, request):
        suffix = hashlib.md5(request.node.name.encode()).hexdigest()[:8]
        admin_headers, _ = _account(client, "adm3_" + suffix, admin=True)
        client.post(
            "/api/safety/report",
            json={"claim_type": "subject_complaint", "contact": "a@b.com"},
        )
        requests_ = client.get(
            "/api/admin/subject-requests", headers=admin_headers
        ).json()["requests"]
        assert len(requests_) == 1

        resp = client.post(
            f"/api/admin/subject-requests/{requests_[0]['id']}",
            json={"status": "actioned", "note": "已删除相关镜像"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        assert (
            client.get("/api/admin/subject-requests", headers=admin_headers).json()[
                "requests"
            ]
            == []
        )


class TestRoleGrant:
    def test_role_defaults_to_user(self, client, request):
        _headers, user_id = _account(
            client, "role_" + hashlib.md5(request.node.name.encode()).hexdigest()[:8]
        )
        from server.auth import get_user_role

        assert get_user_role(user_id) == "user"

    def test_invalid_role_rejected(self, client):
        from server.auth import set_user_role

        with pytest.raises(ValueError):
            set_user_role("whoever", "superuser")

    def test_unknown_user_returns_least_privilege(self, client):
        from server.auth import get_user_role

        assert get_user_role(999999) == "user"
