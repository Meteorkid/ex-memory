"""危机干预在对话入口的接线（FR-011）。

核心断言只有一条：命中危机时，人格 prompt 与 LLM 都**没有被调用**。
不是「先生成再过滤」——「前任」的回应恰恰可能是最危险的那类内容，
所以必须在进入人格模拟之前就短路。

/chat 与 /chat/stream 两条路径各测一遍。M-1 的教训是流式漏了落库，
整个功能形同虚设，这次两条都要钉死。
"""

import hashlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["EX_MEMORY_TEST"] = "1"

CRISIS_TEXT = "我不想活了"
NORMAL_TEXT = "今天天气真好"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    import server.auth as auth
    import server.routes as routes_mod

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()

    noop = MagicMock()
    noop.check = MagicMock()
    monkeypatch.setattr(routes_mod, "_login_limiter", noop)
    # 访问控制不是本文件的被测对象，放行以聚焦闸门本身
    monkeypatch.setattr(routes_mod, "_check_exe_access", lambda slug, uid: slug)
    yield


@pytest.fixture
def client():
    from server.app import create_app

    return TestClient(create_app())


@pytest.fixture
def auth_headers(client, request):
    uname = "crisis_" + hashlib.md5(request.node.name.encode()).hexdigest()[:12]
    client.post(
        "/api/auth/register", json={"username": uname, "password": "test123456"}
    )
    resp = client.post(
        "/api/auth/login", json={"username": uname, "password": "test123456"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


class TestNonStreamPath:
    def test_crisis_message_never_reaches_the_engine(self, client, auth_headers):
        """🔴 FR-011 的核心断言：命中危机时引擎一次都不能被取用。"""
        with patch("server.routes._get_engine") as get_engine:
            resp = client.post(
                "/api/chat",
                json={"slug": "demo", "message": CRISIS_TEXT},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        get_engine.assert_not_called()

    def test_crisis_response_is_platform_identity_not_persona(
        self, client, auth_headers
    ):
        """回复必须以平台通知形式返回，reply 为空，前端才不会当成镜像说的话。"""
        with patch("server.routes._get_engine"):
            resp = client.post(
                "/api/chat",
                json={"slug": "demo", "message": CRISIS_TEXT},
                headers=auth_headers,
            )
        body = resp.json()
        assert body["reply"] == ""
        assert body["notice"]["type"] == "crisis"
        assert body["notice"]["message"].strip()

    def test_unreviewed_content_exposes_no_hotline_numbers(self, client, auth_headers):
        with patch("server.routes._get_engine"):
            resp = client.post(
                "/api/chat",
                json={"slug": "demo", "message": CRISIS_TEXT},
                headers=auth_headers,
            )
        assert resp.json()["notice"]["hotlines"] == []

    def test_normal_message_still_reaches_the_engine(self, client, auth_headers):
        """闸门不能把正常对话也挡掉。"""
        engine = MagicMock()
        engine.chat.return_value = ("回复", [], None)
        with patch("server.routes._get_engine", return_value=engine) as get_engine:
            with patch("server.routes._run_session_archive"):
                resp = client.post(
                    "/api/chat",
                    json={"slug": "demo", "message": NORMAL_TEXT},
                    headers=auth_headers,
                )
        assert resp.status_code == 200
        get_engine.assert_called_once()
        assert resp.json().get("notice") is None


class TestStreamPath:
    def test_crisis_message_never_reaches_the_engine(self, client, auth_headers):
        """🔴 流式路径同样必须短路。"""
        with patch("server.routes._get_engine") as get_engine:
            resp = client.post(
                "/api/chat/stream",
                json={"slug": "demo", "message": CRISIS_TEXT},
                headers=auth_headers,
            )
            body = resp.text
        get_engine.assert_not_called()
        assert resp.status_code == 200
        assert '"type": "crisis"' in body or '"type":"crisis"' in body
        assert "[DONE]" in body

    def test_crisis_event_payload_is_wellformed(self, client, auth_headers):
        with patch("server.routes._get_engine"):
            resp = client.post(
                "/api/chat/stream",
                json={"slug": "demo", "message": CRISIS_TEXT},
                headers=auth_headers,
            )
        payloads = [
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ") and line[6:] != "[DONE]"
        ]
        assert len(payloads) == 1
        assert payloads[0]["type"] == "crisis"
        assert payloads[0]["message"].strip()

    def test_crisis_turn_is_not_persisted_as_conversation(self, client, auth_headers):
        """危机中断不是一轮对话，不应落进对话归档。"""
        with patch("server.routes._get_engine"):
            with patch("server.routes._persist_stream_turn") as persist:
                client.post(
                    "/api/chat/stream",
                    json={"slug": "demo", "message": CRISIS_TEXT},
                    headers=auth_headers,
                )
        persist.assert_not_called()


class TestEventRecording:
    def test_crisis_event_is_recorded_for_review(self, client, auth_headers):
        with patch("server.routes._get_engine"):
            client.post(
                "/api/chat",
                json={"slug": "demo", "message": CRISIS_TEXT},
                headers=auth_headers,
            )
        from server.safety_store import list_pending_reviews

        pending = list_pending_reviews()
        assert len(pending) == 1
        assert pending[0]["event_type"] == "crisis"
        assert pending[0]["action_taken"] == "interrupted"

    def test_recorded_excerpt_is_truncated(self, client, auth_headers):
        """长消息必须被截断，不留长篇上下文。

        注意边界：短消息的片段就等于整条（已脱敏）消息——复核者要看得懂
        才能分辨真实求救与误报。这张表按敏感数据对待，靠权限控制而不是
        靠片段不可读来保护。
        """
        long_crisis = "我最近真的很痛苦" + "，也不知道该跟谁说" * 5 + "，我不想活了"
        with patch("server.routes._get_engine"):
            client.post(
                "/api/chat",
                json={"slug": "demo", "message": long_crisis},
                headers=auth_headers,
            )
        from server.safety_store import list_pending_reviews

        excerpt = list_pending_reviews()[0]["excerpt"]
        assert len(excerpt) <= 30
        assert len(excerpt) < len(long_crisis)

    def test_recorded_excerpt_masks_pii(self, client, auth_headers):
        """片段里的手机号等 PII 必须已脱敏。"""
        with patch("server.routes._get_engine"):
            client.post(
                "/api/chat",
                json={"slug": "demo", "message": "13812345678 我不想活了"},
                headers=auth_headers,
            )
        from server.safety_store import list_pending_reviews

        assert "13812345678" not in list_pending_reviews()[0]["excerpt"]

    def test_review_can_be_resolved(self, client, auth_headers):
        with patch("server.routes._get_engine"):
            client.post(
                "/api/chat",
                json={"slug": "demo", "message": CRISIS_TEXT},
                headers=auth_headers,
            )
        from server.safety_store import list_pending_reviews, resolve_review

        event_id = list_pending_reviews()[0]["id"]
        assert (
            resolve_review(event_id, "reviewer@example.com", "resolved", "已联系")
            is True
        )
        assert list_pending_reviews() == []

    def test_audit_failure_does_not_break_crisis_response(self, client, auth_headers):
        """数据库故障不能连带把危机响应也弄没了。

        故意把事件表删掉来触发真实的 INSERT 失败，走 record_safety_event
        里真正的兜底分支——而不是把这个函数整个换成会抛异常的桩，那样测的
        是桩不是代码。也不能直接 patch 数据库连接：认证同样走它，会在到达
        闸门之前就先挂掉。
        """
        import server.auth as auth

        with auth._get_conn() as conn:
            conn.execute("DROP TABLE safety_events")
            conn.commit()

        with patch("server.routes._get_engine"):
            resp = client.post(
                "/api/chat",
                json={"slug": "demo", "message": CRISIS_TEXT},
                headers=auth_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["notice"]["type"] == "crisis"
