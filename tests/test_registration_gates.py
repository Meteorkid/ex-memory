"""注册准入门槛：手机号实名与年龄确认（FR-020 / FR-022）。

conftest 默认把这两个门槛关掉（多数用例注册只是为了拿身份），
这里显式打开来专门验证。
"""

import hashlib
import os

import pytest
from fastapi.testclient import TestClient

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture
def gates_on(monkeypatch):
    monkeypatch.setattr("config.REQUIRE_PHONE_VERIFICATION", True)
    monkeypatch.setattr("config.REQUIRE_AGE_CONFIRMATION", True)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import server.auth as auth
    import server.routes as routes_mod
    from core.safety import sms
    from server import phone_verify
    from unittest.mock import MagicMock

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()
    noop = MagicMock()
    noop.check = MagicMock()
    monkeypatch.setattr(routes_mod, "_login_limiter", noop)
    phone_verify.reset()

    sent = {}

    class Capturing:
        name = "capture"

        def send(self, phone, code):
            sent[phone] = code
            return True

    sms.set_provider(Capturing())
    from server.app import create_app

    yield TestClient(create_app()), sent
    sms.set_provider(None)
    phone_verify.reset()


def _uname(request):
    return "reg_" + hashlib.md5(request.node.name.encode()).hexdigest()[:12]


class TestAgeGate:
    def test_registration_without_age_confirmation_is_rejected(
        self, client, gates_on, request
    ):
        api, _ = client
        resp = api.post(
            "/api/auth/register",
            json={"username": _uname(request), "password": "test123456"},
        )
        assert resp.status_code == 400
        assert "18" in resp.json()["detail"]


class TestPhoneGate:
    def test_registration_without_phone_is_rejected(self, client, gates_on, request):
        api, _ = client
        resp = api.post(
            "/api/auth/register",
            json={
                "username": _uname(request),
                "password": "test123456",
                "age_confirmed": True,
            },
        )
        assert resp.status_code == 400
        assert "手机号" in resp.json()["detail"]

    def test_registration_with_wrong_code_is_rejected(self, client, gates_on, request):
        api, _ = client
        api.post("/api/auth/phone/send-code", json={"phone": "13800000001"})
        resp = api.post(
            "/api/auth/register",
            json={
                "username": _uname(request),
                "password": "test123456",
                "age_confirmed": True,
                "phone": "13800000001",
                "code": "000000",
            },
        )
        assert resp.status_code == 400
        assert "验证码" in resp.json()["detail"]

    def test_full_flow_succeeds_and_binds_phone(self, client, gates_on, request):
        api, sent = client
        phone = "13800000002"
        assert (
            api.post("/api/auth/phone/send-code", json={"phone": phone}).status_code
            == 200
        )

        resp = api.post(
            "/api/auth/register",
            json={
                "username": _uname(request),
                "password": "test123456",
                "age_confirmed": True,
                "phone": phone,
                "code": sent[phone],
            },
        )
        assert resp.status_code == 200

        from server.auth import _get_conn

        with _get_conn() as conn:
            row = conn.execute(
                "SELECT phone, phone_verified_at, age_confirmed_at FROM users"
                " WHERE username = ?",
                (_uname(request),),
            ).fetchone()
        assert row["phone"] == phone
        assert row["phone_verified_at"]
        assert row["age_confirmed_at"]

    def test_code_cannot_be_reused(self, client, gates_on, request):
        """验证码用过即失效，防止复用注册多个账号。"""
        api, sent = client
        phone = "13800000003"
        api.post("/api/auth/phone/send-code", json={"phone": phone})
        code = sent[phone]
        payload = {
            "password": "test123456",
            "age_confirmed": True,
            "phone": phone,
            "code": code,
        }
        assert (
            api.post(
                "/api/auth/register", json={**payload, "username": "u_a1"}
            ).status_code
            == 200
        )
        # 同一验证码第二次必须失败（手机号占用与验证码失效双重拦截）
        assert (
            api.post(
                "/api/auth/register", json={**payload, "username": "u_a2"}
            ).status_code
            == 400
        )

    def test_duplicate_phone_is_rejected_at_send_code(self, client, gates_on, request):
        api, sent = client
        phone = "13800000004"
        api.post("/api/auth/phone/send-code", json={"phone": phone})
        api.post(
            "/api/auth/register",
            json={
                "username": "u_b1",
                "password": "test123456",
                "age_confirmed": True,
                "phone": phone,
                "code": sent[phone],
            },
        )
        resp = api.post("/api/auth/phone/send-code", json={"phone": phone})
        assert resp.status_code == 400
        assert "已注册" in resp.json()["detail"]

    def test_invalid_phone_format_rejected(self, client, gates_on):
        api, _ = client
        resp = api.post("/api/auth/phone/send-code", json={"phone": "123"})
        assert resp.status_code == 400


class TestCodeLifecycle:
    def test_expired_code_is_rejected(self, client, gates_on):
        from server import phone_verify

        api, sent = client
        phone = "13800000005"
        api.post("/api/auth/phone/send-code", json={"phone": phone})

        # 直接把过期时间拨到过去，比 mock 时钟更直观
        with phone_verify._lock:
            phone_verify._codes[phone]["expires_at"] = 0
        assert phone_verify.verify_code(phone, sent[phone]) is False

    def test_brute_force_invalidates_code(self, client, gates_on):
        from server import phone_verify

        api, sent = client
        phone = "13800000006"
        api.post("/api/auth/phone/send-code", json={"phone": phone})
        for _ in range(phone_verify.MAX_ATTEMPTS + 1):
            phone_verify.verify_code(phone, "999999")
        # 超限后即使给出正确验证码也不再通过
        assert phone_verify.verify_code(phone, sent[phone]) is False

    def test_resend_is_rate_limited(self, client, gates_on):
        api, _ = client
        phone = "13800000007"
        assert (
            api.post("/api/auth/phone/send-code", json={"phone": phone}).status_code
            == 200
        )
        resp = api.post("/api/auth/phone/send-code", json={"phone": phone})
        assert resp.status_code == 429
