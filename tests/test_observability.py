"""可观测性：上下文注入、指标、metrics 端点鉴权（NFR-031 / NFR-032）。"""

import hashlib
import json
import logging
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from core import observability

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture(autouse=True)
def clean_context():
    observability.set_context(trace_id="", request_id="", user_id="", slug="")
    yield
    observability.set_context(trace_id="", request_id="", user_id="", slug="")


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


class TestLogContext:
    def test_filter_injects_context_fields(self):
        observability.set_context(trace_id="t1", user_id=42, slug="xiaoyu")
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
        assert observability.ContextFilter().filter(record) is True
        assert record.trace_id == "t1"
        assert record.user_id == "42"
        assert record.slug == "xiaoyu"

    def test_json_formatter_emits_context(self):
        from core.logging import _JsonFormatter

        observability.set_context(trace_id="t2", request_id="r2")
        record = logging.LogRecord(
            "x", logging.WARNING, __file__, 7, "出事了", None, None
        )
        observability.ContextFilter().filter(record)
        payload = json.loads(_JsonFormatter().format(record))
        assert payload["message"] == "出事了"
        assert payload["trace_id"] == "t2"
        assert payload["level"] == "WARNING"

    def test_empty_context_fields_are_omitted(self):
        from core.logging import _JsonFormatter

        record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
        observability.ContextFilter().filter(record)
        payload = json.loads(_JsonFormatter().format(record))
        assert "trace_id" not in payload


class TestTraceHeaders:
    def test_response_carries_trace_and_request_ids(self, client):
        resp = client.get("/health")
        assert resp.headers["X-Trace-ID"]
        assert resp.headers["X-Request-ID"]

    def test_incoming_trace_id_is_propagated(self, client):
        resp = client.get("/health", headers={"X-Trace-ID": "upstream-trace"})
        assert resp.headers["X-Trace-ID"] == "upstream-trace"


class TestMetricsEndpoint:
    def test_disabled_without_token(self, client, monkeypatch):
        """🔴 指标暴露运营信息，默认公开不可接受。"""
        monkeypatch.setattr("config.METRICS_TOKEN", "")
        assert client.get("/metrics").status_code == 404

    def test_requires_correct_token(self, client, monkeypatch):
        monkeypatch.setattr("config.METRICS_TOKEN", "s3cret")
        assert client.get("/metrics").status_code == 401
        assert (
            client.get(
                "/metrics", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )

    def test_serves_prometheus_text_with_token(self, client, monkeypatch):
        monkeypatch.setattr("config.METRICS_TOKEN", "s3cret")
        resp = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert resp.status_code == 200
        assert "exmemory_http_requests_total" in resp.text


class TestMetricsRecording:
    def test_http_metrics_use_route_template_not_raw_path(self, client, monkeypatch):
        """用原始 path 会让每个 slug 各占一条时间序列，指标基数爆炸。"""
        monkeypatch.setattr("config.METRICS_TOKEN", "s3cret")
        name = "obs_" + hashlib.md5(b"metrics").hexdigest()[:10]
        client.post(
            "/api/auth/register", json={"username": name, "password": "test123456"}
        )
        token = client.post(
            "/api/auth/login", json={"username": name, "password": "test123456"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        for slug in ("aaa", "bbb"):
            client.get(f"/api/exes/{slug}/stats", headers=headers)

        body = client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).text
        assert "/api/exes/{slug}/stats" in body
        assert 'path="/api/exes/aaa/stats"' not in body

    def test_llm_metrics_are_recorded(self):
        observability.init_metrics()
        observability.observe_llm(
            "prov", "success", 0.5, prompt_tokens=10, completion_tokens=5
        )
        from prometheus_client import REGISTRY

        value = REGISTRY.get_sample_value(
            "exmemory_llm_calls_total", {"provider": "prov", "outcome": "success"}
        )
        assert value and value >= 1

    def test_safety_event_metric_is_recorded_even_if_db_write_fails(self, monkeypatch):
        """「发生了多少危机事件」这个信号不能因为落库失败就丢。"""
        observability.init_metrics()
        from prometheus_client import REGISTRY

        import server.safety_store as store

        def boom():
            raise RuntimeError("库挂了")

        monkeypatch.setattr("server.auth._get_conn", boom)
        before = (
            REGISTRY.get_sample_value(
                "exmemory_safety_events_total",
                {"type": "crisis", "action": "interrupted"},
            )
            or 0
        )
        assert store.record_safety_event(1, "crisis", "high", "interrupted") is None
        after = REGISTRY.get_sample_value(
            "exmemory_safety_events_total", {"type": "crisis", "action": "interrupted"}
        )
        assert after == before + 1


class TestTracingDegradation:
    def test_span_works_without_tracer(self):
        """tracer 不可用时退化为纯计时，不影响业务。"""
        observability.reset_for_tests()
        with observability.span("x", foo="bar"):
            pass  # 不抛异常即可

    def test_init_without_endpoint_still_returns_tracer(self):
        observability.reset_for_tests()
        tracer = observability.init_tracing(otlp_endpoint="")
        assert tracer is not False
        observability.reset_for_tests()
