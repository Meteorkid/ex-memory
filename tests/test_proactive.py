"""主动发起对话（FR-071 / FR-072）。

这是让 ta「活着」最关键的一项：改造前完全被动，用户不说话就永远沉默。
但健康保护优先于一切主动性——这个产品的用户群本就有沉溺风险。
"""

import hashlib
import os
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.proactive import (
    TRIGGER_MORNING,
    TRIGGER_NIGHT,
    TRIGGER_SILENCE,
    decide_trigger,
    get_config,
    health_blocks,
    mark_delivered,
    pending_messages,
    queue_message,
    set_config,
)

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import server.auth as auth
    import server.routes as routes_mod

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()
    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    noop = MagicMock()
    noop.check = MagicMock()
    monkeypatch.setattr(routes_mod, "_login_limiter", noop)
    return tmp_path


@pytest.fixture
def client(env):
    from server.app import create_app

    return TestClient(create_app())


def _account(client, request):
    name = "pro_" + hashlib.md5(request.node.name.encode()).hexdigest()[:12]
    client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    token = client.post(
        "/api/auth/login", json={"username": name, "password": "test123456"}
    ).json()["token"]
    from server.auth import validate_token

    return {"Authorization": f"Bearer {token}"}, validate_token(token)


def _enable(slug, owner, **kw):
    return set_config(slug, owner=owner, enabled=True, **kw)


# 锚在「今天」而不是写死日期：日限额是按自然日统计的，入库时间用的是真实时钟，
# 写死一个过去的日期会让限额测试只在那一天成立。
_TODAY = date.today()
MORNING = datetime.combine(_TODAY, time(9, 0))
NIGHT = datetime.combine(_TODAY, time(22, 0))
QUIET = datetime.combine(_TODAY, time(3, 0))


class TestDefaultOff:
    def test_disabled_by_default(self, env):
        """主动消息是打扰，得用户明确开启。"""
        assert get_config("s", 1)["enabled"] is False

    def test_disabled_yields_no_trigger(self, env):
        assert decide_trigger("s", 1, owner=1, now=MORNING) is None


class TestTriggers:
    def test_morning(self, env):
        _enable("s", 1)
        assert decide_trigger("s", 1, owner=1, now=MORNING) == TRIGGER_MORNING

    def test_night(self, env):
        _enable("s", 1)
        assert decide_trigger("s", 1, owner=1, now=NIGHT) == TRIGGER_NIGHT

    def test_silence_wins_over_time_of_day(self, env):
        """好几天没聊比「早安」更值得说。"""
        _enable("s", 1)
        long_ago = (MORNING - timedelta(days=5)).isoformat()
        assert (
            decide_trigger("s", 1, owner=1, last_seen_iso=long_ago, now=MORNING)
            == TRIGGER_SILENCE
        )

    def test_recent_chat_is_not_silence(self, env):
        _enable("s", 1)
        recent = (MORNING - timedelta(hours=2)).isoformat()
        assert (
            decide_trigger("s", 1, owner=1, last_seen_iso=recent, now=MORNING)
            == TRIGGER_MORNING
        )

    def test_no_trigger_at_random_hours(self, env):
        _enable("s", 1)
        assert (
            decide_trigger("s", 1, owner=1, now=datetime.combine(_TODAY, time(15, 0)))
            is None
        )

    def test_malformed_last_seen_is_tolerated(self, env):
        _enable("s", 1)
        assert (
            decide_trigger("s", 1, owner=1, last_seen_iso="不是时间", now=MORNING)
            == TRIGGER_MORNING
        )


class TestQuietHours:
    def test_quiet_window_blocks(self, env):
        _enable("s", 1)
        assert decide_trigger("s", 1, owner=1, now=QUIET) is None

    def test_quiet_window_spanning_midnight(self, env):
        _enable("s", 1, quiet_start=23, quiet_end=8)
        assert (
            decide_trigger("s", 1, owner=1, now=datetime.combine(_TODAY, time(23, 30)))
            is None
        )
        assert (
            decide_trigger("s", 1, owner=1, now=datetime.combine(_TODAY, time(7, 0)))
            is None
        )

    def test_non_spanning_quiet_window(self, env):
        _enable("s", 1, quiet_start=8, quiet_end=10)
        assert decide_trigger("s", 1, owner=1, now=MORNING) is None


class TestDailyCap:
    def test_cap_is_enforced(self, env):
        _enable("s", 1, max_per_day=1)
        queue_message("s", 1, TRIGGER_MORNING, "早", owner=1)
        assert decide_trigger("s", 1, owner=1, now=MORNING) is None

    def test_cap_holds_across_utc_day_boundary(self, env):
        """本地日期与 UTC 日期不同的那一段，日限额同样要生效。

        created_at 按 UTC 落库，早先的实现拿本地日期去 LIKE。西半球部署时，
        本地晚上的推送窗口（21–23 点）落在 UTC 的第二天，于是每晚的消息一条
        都统计不到，日限额整段失效——对一个默认就要防沉溺的功能，这是最不能
        漏的一处。用带固定偏移的 aware 时间构造，不依赖跑测试的机器在哪个时区。
        """
        from server.auth import _get_conn

        est = timezone(timedelta(hours=-5))
        local_now = datetime(2026, 9, 7, 22, 30, tzinfo=est)  # UTC 已是 09-08 03:30
        _enable("s", 1, max_per_day=1)
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO proactive_messages"
                " (exe_key, user_id, slug, trigger, content, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("1/s", 1, "s", TRIGGER_NIGHT, "睡了吗", "2026-09-08 03:00:00"),
            )
            conn.commit()

        assert decide_trigger("s", 1, owner=1, now=local_now) is None

    def test_zero_cap_disables_effectively(self, env):
        _enable("s", 1, max_per_day=0)
        assert decide_trigger("s", 1, owner=1, now=MORNING) is None

    def test_invalid_cap_rejected(self, env):
        with pytest.raises(ValueError):
            set_config("s", owner=1, max_per_day=99)

    def test_invalid_quiet_hour_rejected(self, env):
        with pytest.raises(ValueError):
            set_config("s", owner=1, quiet_start=30)


class TestHealthInterlock:
    """🔴 健康保护优先于一切主动性。"""

    def test_cooldown_blocks_proactive(self, client, request, env):
        from server.auth import _get_conn

        _headers, user_id = _account(client, request)
        _enable("s", user_id)
        today = datetime.now().strftime("%Y-%m-%d")
        until = (datetime.now() + timedelta(hours=1)).isoformat()
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO user_activity (user_id, activity_date, active_seconds,"
                " last_active_at, cooldown_until) VALUES (?, ?, ?, ?, ?)",
                (user_id, today, 999999, datetime.now().isoformat(), until),
            )
            conn.commit()

        assert health_blocks(user_id) is not None
        assert decide_trigger("s", user_id, owner=user_id, now=MORNING) is None

    def test_recent_crisis_blocks_proactive(self, client, request, env):
        """刚经历过危机的人最不该收到「ta 突然发来一条消息」。"""
        from server.safety_store import record_safety_event

        _headers, user_id = _account(client, request)
        _enable("s", user_id)
        record_safety_event(user_id, "crisis", "high", "interrupted")

        assert health_blocks(user_id) is not None
        assert decide_trigger("s", user_id, owner=user_id, now=MORNING) is None

    def test_healthy_user_is_allowed(self, client, request, env):
        _headers, user_id = _account(client, request)
        _enable("s", user_id)
        assert health_blocks(user_id) is None
        assert (
            decide_trigger("s", user_id, owner=user_id, now=MORNING) == TRIGGER_MORNING
        )

    def test_unknown_state_is_treated_conservatively(self, env, monkeypatch):
        """查不到状态时保守跳过，不推。"""

        def boom(_user_id):
            raise RuntimeError("库挂了")

        monkeypatch.setattr("server.usage_guard.status", boom)
        assert health_blocks(1) is not None


class TestQueueAndDelivery:
    def test_queue_and_fetch(self, env):
        queue_message("s", 1, TRIGGER_MORNING, "早呀", owner=1)
        pending = pending_messages(1, "s")
        assert len(pending) == 1 and pending[0]["content"] == "早呀"

    def test_empty_content_is_not_queued(self, env):
        assert queue_message("s", 1, TRIGGER_MORNING, "   ", owner=1) is None

    def test_mark_delivered_removes_from_pending(self, env):
        message_id = queue_message("s", 1, TRIGGER_MORNING, "早", owner=1)
        assert mark_delivered([message_id], 1) == 1
        assert pending_messages(1, "s") == []

    def test_cannot_mark_someone_elses_message(self, env):
        message_id = queue_message("s", 1, TRIGGER_MORNING, "早", owner=1)
        assert mark_delivered([message_id], 999) == 0
        assert len(pending_messages(1, "s")) == 1

    def test_users_are_isolated(self, env):
        queue_message("s", 1, TRIGGER_MORNING, "给用户1", owner=1)
        assert pending_messages(2) == []


class TestCompose:
    def test_uses_same_engine_so_persona_stays_consistent(self, env):
        """另写一套模板会让主动消息一眼看出是系统发的。"""
        from core.proactive import compose

        engine = MagicMock()
        engine.chat.return_value = ("在干嘛呢", [], None)
        assert compose(engine, TRIGGER_MORNING) == "在干嘛呢"
        prompt = engine.chat.call_args[0][0]
        assert "早上" in prompt
        assert "不要解释你在做什么" in prompt


class TestApi:
    def test_config_roundtrip(self, client, request, env):
        headers, _user_id = _account(client, request)
        with patch("server.routes._check_exe_access", lambda slug, uid: slug):
            resp = client.post(
                "/api/exes/demo/proactive/config",
                json={"enabled": True, "max_per_day": 3},
                headers=headers,
            )
            assert resp.status_code == 200
            body = client.get("/api/exes/demo/proactive", headers=headers).json()
        assert body["config"]["enabled"] is True
        assert body["config"]["max_per_day"] == 3

    def test_pending_messages_surface_via_api(self, client, request, env):
        headers, user_id = _account(client, request)
        queue_message("demo", user_id, TRIGGER_NIGHT, "早点睡", owner=user_id)
        with patch("server.routes._check_exe_access", lambda slug, uid: slug):
            body = client.get("/api/exes/demo/proactive", headers=headers).json()
        assert body["pending"][0]["content"] == "早点睡"

    def test_deliver_marks_as_read(self, client, request, env):
        headers, user_id = _account(client, request)
        message_id = queue_message(
            "demo", user_id, TRIGGER_NIGHT, "睡了吗", owner=user_id
        )
        with patch("server.routes._check_exe_access", lambda slug, uid: slug):
            client.post(
                "/api/exes/demo/proactive/deliver",
                json={"message_ids": [message_id]},
                headers=headers,
            )
            body = client.get("/api/exes/demo/proactive", headers=headers).json()
        assert body["pending"] == []

    def test_invalid_config_rejected(self, client, request, env):
        headers, _ = _account(client, request)
        with patch("server.routes._check_exe_access", lambda slug, uid: slug):
            resp = client.post(
                "/api/exes/demo/proactive/config",
                json={"max_per_day": 50},
                headers=headers,
            )
        assert resp.status_code == 422 or resp.status_code == 400
