"""使用强度保护与 AI 生成内容标识（FR-021 / FR-023）。"""

import hashlib
import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

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
    monkeypatch.setattr(routes_mod, "_check_exe_access", lambda slug, uid: slug)

    from server.app import create_app

    return TestClient(create_app())


@pytest.fixture
def auth_headers(client, request):
    uname = "usage_" + hashlib.md5(request.node.name.encode()).hexdigest()[:12]
    client.post(
        "/api/auth/register", json={"username": uname, "password": "test123456"}
    )
    resp = client.post(
        "/api/auth/login", json={"username": uname, "password": "test123456"}
    )
    token = resp.json()["token"]
    from server.auth import validate_token

    return {"Authorization": f"Bearer {token}"}, validate_token(token)


def _force_cooldown(user_id, minutes=30):
    """把用户直接置于冷静期，免去真的聊三小时。"""
    from server.auth import _get_conn

    today = datetime.now().strftime("%Y-%m-%d")
    until = (datetime.now() + timedelta(minutes=minutes)).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_activity"
            " (user_id, activity_date, active_seconds, last_active_at, cooldown_until)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, today, 999999, datetime.now().isoformat(), until),
        )
        conn.commit()


class TestAccumulation:
    def test_state_is_persisted_not_in_memory(self, client, auth_headers):
        """回归：原实现放在进程内字典，重启清零、多设备各算各的。"""
        _headers, user_id = auth_headers
        from server.usage_guard import status, touch

        touch(user_id)
        from server.auth import _get_conn

        with _get_conn() as conn:
            row = conn.execute(
                "SELECT active_seconds FROM user_activity WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        assert row is not None and row["active_seconds"] > 0
        assert status(user_id)["active_seconds"] > 0

    def test_long_gap_counts_as_fresh_interaction_not_elapsed_time(
        self, client, auth_headers
    ):
        """中午聊两句、晚上再聊两句，中间几小时不能算成使用时长。"""
        from server.auth import _get_conn
        from server.usage_guard import touch

        _headers, user_id = auth_headers
        touch(user_id)
        long_ago = (datetime.now() - timedelta(hours=6)).isoformat()
        with _get_conn() as conn:
            conn.execute(
                "UPDATE user_activity SET last_active_at = ? WHERE user_id = ?",
                (long_ago, user_id),
            )
            conn.commit()

        before = touch(user_id)["active_seconds"]
        assert before < 300, "长间隔被当成了连续使用"


class TestChatGate:
    def test_cooldown_blocks_normal_chat(self, client, auth_headers):
        headers, user_id = auth_headers
        _force_cooldown(user_id)

        with patch("server.routes._get_engine") as get_engine:
            resp = client.post(
                "/api/chat",
                json={"slug": "demo", "message": "在吗"},
                headers=headers,
            )
        assert resp.json()["notice"]["type"] == "usage_limit"
        get_engine.assert_not_called()

    def test_crisis_still_reaches_user_during_cooldown(self, client, auth_headers):
        """🔴 达到时长上限的用户仍然必须拿得到危机响应。"""
        headers, user_id = auth_headers
        _force_cooldown(user_id)

        with patch("server.routes._get_engine"):
            resp = client.post(
                "/api/chat",
                json={"slug": "demo", "message": "我不想活了"},
                headers=headers,
            )
        assert resp.json()["notice"]["type"] == "crisis"

    def test_cooldown_blocks_stream_path_too(self, client, auth_headers):
        headers, user_id = auth_headers
        _force_cooldown(user_id)

        with patch("server.routes._get_engine") as get_engine:
            resp = client.post(
                "/api/chat/stream",
                json={"slug": "demo", "message": "在吗"},
                headers=headers,
            )
        get_engine.assert_not_called()
        assert "usage_limit" in resp.text

    def test_normal_user_is_not_blocked(self, client, auth_headers):
        headers, _user_id = auth_headers
        engine = MagicMock()
        engine.chat.return_value = ("回复", [], None)
        with patch("server.routes._get_engine", return_value=engine):
            with patch("server.routes._run_session_archive"):
                resp = client.post(
                    "/api/chat",
                    json={"slug": "demo", "message": "在吗"},
                    headers=headers,
                )
        assert resp.json().get("notice") is None


class TestHealthStatsEndpoint:
    def test_reports_persisted_usage(self, client, auth_headers):
        headers, user_id = auth_headers
        from server.usage_guard import touch

        touch(user_id)
        body = client.get("/api/user/health/stats", headers=headers).json()
        assert body["active_seconds"] > 0
        assert body["daily_limit_seconds"] > 0
        assert body["in_cooldown"] is False


class TestAiGeneratedLabel:
    def test_chat_page_carries_persistent_label(self):
        """FR-021：对话界面常驻 AI 生成标识。"""
        from pathlib import Path

        html = (Path("web") / "static" / "index.html").read_text(encoding="utf-8")
        assert 'id="ai-notice"' in html
        assert "由 AI 生成" in html

    def test_account_export_carries_declaration(
        self, tmp_path, monkeypatch, client, auth_headers
    ):
        import zipfile

        import server.account_lifecycle as lifecycle

        _headers, user_id = auth_headers
        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        monkeypatch.setattr(lifecycle, "_feedback_path", lambda: tmp_path / "fb.jsonl")

        zip_path = lifecycle.export_account(user_id)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                manifest = json.loads(zf.read("account.json"))
            assert "AI 生成" in manifest["ai_generated_notice"]
        finally:
            zip_path.unlink(missing_ok=True)
