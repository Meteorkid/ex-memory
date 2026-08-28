"""server/middleware.py 测试：限流、IP 提取、登录限流。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def make_app():
    app = FastAPI()

    @app.get("/ok")
    def ok():
        return {"status": "ok"}

    return app


def _make_request(xff: bytes | None = None, client=("127.0.0.1", 12345)):
    """构造一个带/不带 X-Forwarded-For 的 Request。"""
    from fastapi import Request as FR

    async def dummy_receive():
        return {"type": "http.request"}

    headers = [(b"x-forwarded-for", xff)] if xff else []
    return FR({"type": "http", "headers": headers, "client": client}, dummy_receive)


class TestGetClientIP:
    def test_x_forwarded_for(self, monkeypatch):
        """直连来源在白名单内时，采信 XFF 的第一跳。"""
        monkeypatch.setattr("config.TRUSTED_PROXY", True)
        monkeypatch.setattr("config.TRUSTED_PROXY_IPS", {"127.0.0.1"})
        from server.middleware import _get_client_ip

        assert _get_client_ip(_make_request(b"10.0.0.1, 10.0.0.2")) == "10.0.0.1"

    def test_empty_whitelist_ignores_forwarded_header(self, monkeypatch):
        """回归：TRUSTED_PROXY 开启但白名单为空时必须 fail-closed。

        旧实现会无条件采信 XFF，攻击者轮换该头即可绕过全部限流。
        """
        monkeypatch.setattr("config.TRUSTED_PROXY", True)
        monkeypatch.setattr("config.TRUSTED_PROXY_IPS", set())
        import server.middleware as mw
        monkeypatch.setattr(mw, "_warned_empty_proxy_whitelist", False)

        # 无论伪造成什么，限流 key 都应回落到真实直连 IP
        for spoof in (b"1.1.1.1", b"2.2.2.2", b"3.3.3.3"):
            assert mw._get_client_ip(_make_request(spoof)) == "127.0.0.1"

    def test_untrusted_direct_source_ignores_forwarded_header(self, monkeypatch):
        """回归：请求绕过代理直连时，其 XFF 不可信。"""
        monkeypatch.setattr("config.TRUSTED_PROXY", True)
        monkeypatch.setattr("config.TRUSTED_PROXY_IPS", {"10.9.9.9"})
        from server.middleware import _get_client_ip

        req = _make_request(b"1.1.1.1", client=("203.0.113.7", 4444))
        assert _get_client_ip(req) == "203.0.113.7"

    def test_trusted_proxy_disabled_ignores_forwarded_header(self, monkeypatch):
        """未开启 TRUSTED_PROXY 时永不采信 XFF。"""
        monkeypatch.setattr("config.TRUSTED_PROXY", False)
        from server.middleware import _get_client_ip

        assert _get_client_ip(_make_request(b"1.1.1.1")) == "127.0.0.1"

    def test_direct_client(self):
        from server.middleware import _get_client_ip
        from fastapi import Request as FR

        async def dummy_receive():
            return {"type": "http.request"}

        scope = {
            "type": "http",
            "headers": [],
            "client": ("192.168.1.1", 12345),
        }
        request = FR(scope, dummy_receive)
        assert _get_client_ip(request) == "192.168.1.1"

    def test_no_client(self):
        from server.middleware import _get_client_ip
        from fastapi import Request as FR

        async def dummy_receive():
            return {"type": "http.request"}

        scope = {"type": "http", "headers": [], "client": None}
        request = FR(scope, dummy_receive)
        assert _get_client_ip(request) == "unknown"


class TestRateLimiter:
    def test_allows_requests_within_limit(self):
        from server.middleware import RateLimiter

        limiter = RateLimiter(max_requests=5, window_seconds=60)
        app = make_app()
        app.middleware("http")(limiter)
        client = TestClient(app)

        for _ in range(5):
            r = client.get("/ok")
            assert r.status_code == 200

    def test_blocks_over_limit(self):
        from server.middleware import RateLimiter

        limiter = RateLimiter(max_requests=3, window_seconds=60)
        app = make_app()
        app.middleware("http")(limiter)
        client = TestClient(app)

        for _ in range(3):
            r = client.get("/ok")
            assert r.status_code == 200

        r = client.get("/ok")
        assert r.status_code == 429


class TestLoginRateLimiter:
    def test_allows_within_limit(self):
        from server.middleware import LoginRateLimiter

        limiter = LoginRateLimiter(max_per_user=5, max_per_ip=15)
        for _ in range(5):
            limiter.check("testuser", "10.0.0.1")

    def test_blocks_user_after_limit(self):
        from server.middleware import LoginRateLimiter, HTTPException

        limiter = LoginRateLimiter(max_per_user=3, max_per_ip=15)
        for _ in range(3):
            limiter.check("testuser", "10.0.0.1")
        with pytest.raises(HTTPException) as exc:
            limiter.check("testuser", "10.0.0.1")
        assert exc.value.status_code == 429

    def test_blocks_ip_after_limit(self):
        from server.middleware import LoginRateLimiter, HTTPException

        limiter = LoginRateLimiter(max_per_user=15, max_per_ip=3)
        for idx in range(3):
            limiter.check(f"user{idx}", "10.0.0.1")
        with pytest.raises(HTTPException) as exc:
            limiter.check("another_user", "10.0.0.1")
        assert exc.value.status_code == 429

    def test_different_users_different_limits(self):
        from server.middleware import LoginRateLimiter, HTTPException

        limiter = LoginRateLimiter(max_per_user=3, max_per_ip=15)
        for _ in range(3):
            limiter.check("user_a", "10.0.0.1")
        # user_a should be blocked
        with pytest.raises(HTTPException):
            limiter.check("user_a", "10.0.0.1")
        # user_b should still be allowed
        limiter.check("user_b", "10.0.0.1")


class TestRequestLoggingMiddleware:
    def test_adds_request_id(self):
        from server.middleware import RequestLoggingMiddleware

        app = make_app()
        app.middleware("http")(RequestLoggingMiddleware())
        client = TestClient(app)
        r = client.get("/ok")
        assert r.status_code == 200
        assert "X-Request-ID" in r.headers

    def test_preserves_incoming_request_id(self):
        from server.middleware import RequestLoggingMiddleware

        app = make_app()
        app.middleware("http")(RequestLoggingMiddleware())
        client = TestClient(app)
        r = client.get("/ok", headers={"X-Request-ID": "my-custom-id"})
        assert r.headers["X-Request-ID"] == "my-custom-id"
