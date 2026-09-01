"""内容审核：本地兜底、主通道降级、以及流式的跨分块保证。"""

import hashlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.safety.moderation import (
    ModerationResult,
    moderate_input,
    moderate_output,
    set_provider,
)

os.environ["EX_MEMORY_TEST"] = "1"

BANNED = "代开发票"  # 种子词表里的条目


@pytest.fixture(autouse=True)
def no_provider():
    """默认不挂主通道，测的是本地兜底行为。"""
    set_provider(None)
    yield
    set_provider(None)


class TestLocalFallback:
    def test_blocks_seeded_term(self):
        assert moderate_input(f"请问有人{BANNED}吗").allowed is False

    def test_allows_normal_text(self):
        assert moderate_input("今天天气真好").allowed is True

    def test_output_direction_uses_same_wordlist(self):
        assert moderate_output(f"我可以帮你{BANNED}").allowed is False

    def test_missing_wordlist_does_not_crash(self, monkeypatch, tmp_path):
        """词表不可用时兜底通道为空，但不能把对话链路弄挂。"""
        from core.safety import moderation

        monkeypatch.setattr(moderation, "WORDLIST_PATH", tmp_path / "missing.json")
        moderation.reset_wordlist_cache()
        assert moderate_input("任意文本").allowed is True
        moderation.reset_wordlist_cache()


class TestProviderDegradation:
    def test_provider_failure_falls_back_to_local_not_open(self):
        """🔴 主通道故障必须降级到本地词表，不能静默放行。"""

        class Broken:
            name = "broken"

            def check(self, text, direction):
                raise RuntimeError("服务不可用")

        set_provider(Broken())
        assert moderate_input(f"有人{BANNED}吗").allowed is False
        assert moderate_input("今天天气真好").allowed is True

    def test_provider_block_is_honored(self):
        class Strict:
            name = "strict"

            def check(self, text, direction):
                return ModerationResult(
                    allowed=False, category="test", severity="block", detector=self.name
                )

        set_provider(Strict())
        result = moderate_input("本地词表放行但主通道拦截")
        assert result.allowed is False
        assert result.detector == "strict"

    def test_local_catches_what_provider_misses(self):
        """主通道漏判时本地表还能兜一层。"""

        class Permissive:
            name = "permissive"

            def check(self, text, direction):
                return ModerationResult.ok(self.name)

        set_provider(Permissive())
        assert moderate_input(f"有人{BANNED}吗").allowed is False


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
    uname = "mod_" + hashlib.md5(request.node.name.encode()).hexdigest()[:12]
    client.post(
        "/api/auth/register", json={"username": uname, "password": "test123456"}
    )
    resp = client.post(
        "/api/auth/login", json={"username": uname, "password": "test123456"}
    )
    return {"Authorization": f"Bearer {resp.json()['token']}"}


class TestRouteWiring:
    def test_blocked_input_never_reaches_engine(self, client, auth_headers):
        with patch("server.routes._get_engine") as get_engine:
            resp = client.post(
                "/api/chat",
                json={"slug": "demo", "message": f"有人{BANNED}吗"},
                headers=auth_headers,
            )
        get_engine.assert_not_called()
        assert resp.json()["notice"]["type"] == "blocked"

    def test_crisis_takes_priority_over_content_block(self, client, auth_headers):
        """🔴 既命中危机又命中违规词时走危机流程，不能把求救的人挡回去。"""
        with patch("server.routes._get_engine"):
            resp = client.post(
                "/api/chat",
                json={"slug": "demo", "message": f"教你怎么自杀 {BANNED}"},
                headers=auth_headers,
            )
        assert resp.json()["notice"]["type"] == "crisis"

    def test_blocked_output_is_not_returned(self, client, auth_headers):
        engine = MagicMock()
        engine.chat.return_value = (f"我可以帮你{BANNED}", [], None)
        with patch("server.routes._get_engine", return_value=engine):
            with patch("server.routes._run_session_archive"):
                resp = client.post(
                    "/api/chat",
                    json={"slug": "demo", "message": "你好"},
                    headers=auth_headers,
                )
        body = resp.json()
        assert body["reply"] == ""
        assert body["notice"]["type"] == "blocked"
        assert BANNED not in json.dumps(body, ensure_ascii=False)


class TestStreamHoldbackGuarantee:
    def test_banned_term_split_across_chunks_never_fully_sent(
        self, client, auth_headers
    ):
        """🔴 核心保证：违规词即使被拆到多个分块，也不会完整到达客户端。

        尾部保留长度不小于词表最长词，所以违规词被检出时它的尾部必然
        还扣在服务端没发出去。
        """
        engine = MagicMock()

        def fake_stream(message, history):
            yield {"type": "text", "content": "我跟你说" * 5}  # 先制造已下发内容
            yield {"type": "text", "content": BANNED[:2]}
            yield {"type": "text", "content": BANNED[2:]}
            yield {"type": "text", "content": "后面还有很多"}

        engine.chat_stream.side_effect = fake_stream

        with patch("server.routes._get_engine", return_value=engine):
            with patch("server.routes._run_session_archive"):
                resp = client.post(
                    "/api/chat/stream",
                    json={"slug": "demo", "message": "你好"},
                    headers=auth_headers,
                )

        events = [
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ") and line[6:] != "[DONE]"
        ]
        sent_text = "".join(
            e.get("content", "") for e in events if e.get("type") == "text"
        )
        assert BANNED not in sent_text, "违规词完整到达了客户端"
        assert any(e.get("type") == "blocked" for e in events), "未下发拦截通知"

    def test_clean_stream_delivers_full_text(self, client, auth_headers):
        """扣住的尾部必须在收尾时放出来，不能吞掉内容。"""
        engine = MagicMock()
        full = "今天天气真好我们出去走走吧路上还能拍点照片"

        def fake_stream(message, history):
            yield {"type": "text", "content": full}

        engine.chat_stream.side_effect = fake_stream

        with patch("server.routes._get_engine", return_value=engine):
            with patch("server.routes._run_session_archive"):
                resp = client.post(
                    "/api/chat/stream",
                    json={"slug": "demo", "message": "你好"},
                    headers=auth_headers,
                )
        events = [
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ") and line[6:] != "[DONE]"
        ]
        sent = "".join(e.get("content", "") for e in events if e.get("type") == "text")
        assert sent == full


class TestLateProviderCheck:
    def test_provider_block_after_stream_retracts_and_skips_persist(
        self, client, auth_headers
    ):
        """本地表放行、主通道判违规时：流末补检、撤回、且不落库。

        这是流式与「违规内容一个字都不到前端」无法两全的地方——本地表挡
        已知词，主通道兜其余，代价是后者只能事后撤回。
        """

        class Strict:
            name = "strict"

            def check(self, text, direction):
                from core.safety.moderation import DIRECTION_OUTPUT, ModerationResult

                if direction == DIRECTION_OUTPUT:
                    return ModerationResult(
                        allowed=False,
                        category="late",
                        severity="block",
                        detector=self.name,
                    )
                return ModerationResult.ok(self.name)

        set_provider(Strict())
        engine = MagicMock()

        def fake_stream(message, history):
            yield {"type": "text", "content": "本地词表放行的普通文本"}

        engine.chat_stream.side_effect = fake_stream

        with patch("server.routes._get_engine", return_value=engine):
            with patch("server.routes._run_session_archive"):
                with patch("server.routes._persist_stream_turn") as persist:
                    resp = client.post(
                        "/api/chat/stream",
                        json={"slug": "demo", "message": "你好"},
                        headers=auth_headers,
                    )

        events = [
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ") and line[6:] != "[DONE]"
        ]
        assert any(e.get("type") == "blocked" for e in events), "主通道拦截未下发通知"
        # 落库调用仍会发生，但内容已被清空，不留违规文本
        persisted = persist.call_args[0][3] if persist.call_args else ""
        assert persisted == ""


class TestFlagSeverity:
    def test_flagged_category_is_allowed_but_recorded(self, client, auth_headers):
        """severity=flag 放行但留痕——词表 README 承诺了这个行为。"""
        from unittest.mock import patch as _patch

        engine = MagicMock()
        engine.chat.return_value = ("好的", [], None)
        with _patch("server.routes._get_engine", return_value=engine):
            with _patch("server.routes._run_session_archive"):
                resp = client.post(
                    "/api/chat",
                    json={"slug": "demo", "message": "你能不能冒充本人跟他说话"},
                    headers=auth_headers,
                )
        # 放行：拿到了正常回复而不是拦截通知
        assert resp.json().get("notice") is None
        assert resp.json()["reply"] == "好的"

        from server.auth import _get_conn

        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT action_taken, severity, detector FROM safety_events"
                " WHERE event_type = 'content_input'"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["action_taken"] == "flagged"
        assert rows[0]["severity"] == "flag"
        assert "impersonation" in rows[0]["detector"]

    def test_clean_message_records_nothing(self, client, auth_headers):
        from unittest.mock import patch as _patch

        engine = MagicMock()
        engine.chat.return_value = ("好的", [], None)
        with _patch("server.routes._get_engine", return_value=engine):
            with _patch("server.routes._run_session_archive"):
                client.post(
                    "/api/chat",
                    json={"slug": "demo", "message": "今天天气真好"},
                    headers=auth_headers,
                )
        from server.auth import _get_conn

        with _get_conn() as conn:
            assert conn.execute("SELECT COUNT(*) FROM safety_events").fetchone()[0] == 0
