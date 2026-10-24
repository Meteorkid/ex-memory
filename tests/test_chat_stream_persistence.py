"""D-02 回归：流式对话落库（含客户端中断场景）。"""

import asyncio
import json
import traceback
import pytest
from unittest.mock import patch, MagicMock
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    """独立用户库 + 独立 exes 目录。"""
    import server.auth as auth

    test_db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", test_db)
    monkeypatch.setattr(auth, "DB_DIR", test_db.parent)
    with auth._get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS tokens")
        conn.execute("DROP TABLE IF EXISTS users")
        conn.commit()
    auth.init_db()

    exes = tmp_path / "exes"
    exes.mkdir()
    monkeypatch.setattr("config.EXES_DIR", exes)
    monkeypatch.setattr("config.SINGLE_USER_MODE", False)

    import server.routes as routes

    monkeypatch.setattr(routes, "_login_limiter", None)
    yield exes
    routes._engine_cache.clear()


def _make_exe(exes_dir, slug: str, owner: int = 1):
    ex_dir = exes_dir / slug
    ex_dir.mkdir(parents=True, exist_ok=True)
    (ex_dir / "meta.json").write_text(
        json.dumps({"name": slug, "slug": slug, "owner_user_id": owner}),
        encoding="utf-8",
    )


def _login(client, username="streamer", password="pass1234"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _read_turns(exes_dir, slug: str) -> list[dict]:
    path = exes_dir / slug / "conversations" / "conversation.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _mock_engine(chunks: list[dict]) -> MagicMock:
    engine = MagicMock()

    def fake_stream(message, history):
        yield from chunks

    engine.chat_stream.side_effect = fake_stream
    return engine


class TestStreamPersists:
    def test_stream_completes_and_persists_turn(self, env, client_from_env):
        client, headers = client_from_env
        _make_exe(env, "s1")
        engine = _mock_engine(
            [
                {"type": "text", "content": "在吗"},
                {"type": "text", "content": "刚看到 [sticker:builtin_happy_laugh]"},
                {"type": "sticker", "id": "builtin_happy_laugh"},
            ]
        )

        with patch("server.routes._get_engine", return_value=engine):
            resp = client.post(
                "/api/chat/stream",
                json={"slug": "s1", "message": "hi"},
                headers=headers,
            )

        assert resp.status_code == 200
        assert "[DONE]" in resp.text

        turns = _read_turns(env, "s1")
        assert len(turns) == 2, "回归：流式对话未落库"
        user_msg, assistant_msg = turns
        assert user_msg["role"] == "user"
        assert user_msg["content"] == "hi"
        assert user_msg["source"] == "web"
        assert assistant_msg["role"] == "assistant"
        # 口径与 /chat 一致：贴纸标签剥离、贴纸单独存
        assert "[sticker:" not in assistant_msg["content"]
        assert "刚看到" in assistant_msg["content"]
        assert assistant_msg["stickers"] == ["builtin_happy_laugh"]

    def test_persist_failure_does_not_break_stream(self, env, client_from_env):
        client, headers = client_from_env
        _make_exe(env, "s2")
        engine = _mock_engine([{"type": "text", "content": "你好"}])

        with (
            patch("server.routes._get_engine", return_value=engine),
            patch(
                "core.conversation_store.append_turn",
                side_effect=OSError("disk full"),
            ),
        ):
            resp = client.post(
                "/api/chat/stream",
                json={"slug": "s2", "message": "hi"},
                headers=headers,
            )

        assert resp.status_code == 200
        assert "[DONE]" in resp.text, "回归：落库失败不应影响流式响应"


class TestStreamInterrupted:
    def test_incomplete_native_response_discards_partial_turn_and_refunds(self, env, client_from_env):
        from core.billing import balance_status, ensure_account, quota_status
        from core.model_client import IncompleteModelResponse, _safe_stream
        from server.auth import _get_conn
        import server.routes as routes

        client, headers = client_from_env
        _make_exe(env, "native_incomplete")
        account_id = ensure_account(1)
        before = balance_status(account_id)["balance_micros"]

        def native_stream():
            yield {"type": "text", "content": "不完整的回复开头"}
            yield {"type": "sticker", "id": "builtin_happy_laugh"}
            yield {"type": "usage", "prompt_tokens": 10, "completion_tokens": 4}
            raise IncompleteModelResponse()

        engine = MagicMock()
        engine.chat_stream.side_effect = lambda *args: _safe_stream(native_stream())
        with (
            patch("server.routes._get_engine", return_value=engine),
            patch("server.routes._run_session_archive") as archive,
            patch("server.routes.settle", wraps=routes.settle) as settle,
        ):
            response = client.post(
                "/api/chat/stream",
                json={"slug": "native_incomplete", "message": "合成问题"},
                headers=headers,
            )
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert any(event.get("content") == "不完整的回复开头" for event in events)
        assert any("error" in event for event in events)
        assert "[DONE]" not in response.text
        assert _read_turns(env, "native_incomplete") == []
        archive.assert_not_called()
        settle.assert_not_called()
        assert balance_status(account_id)["balance_micros"] == before
        assert quota_status(account_id)["turns_used"] == 0
        with _get_conn() as conn:
            assert conn.execute("SELECT count(*) AS n FROM usage_records WHERE account_id = ?", (account_id,)).fetchone()["n"] == 0

    def test_client_disconnect_persists_partial_reply(self, env):
        """客户端中断（GeneratorExit）时，已生成部分必须落库。

        分块要长于 _HOLDBACK_CHARS，否则尾部保留策略下循环中不会下发任何
        事件，测不出「生成到一半被打断」这个场景。
        """
        _make_exe(env, "s3")
        engine = _mock_engine(
            [
                {"type": "text", "content": "第一段" * 10},
                {"type": "text", "content": "第二段" * 10},
                {"type": "text", "content": "第三段" * 10},
            ]
        )

        from server.models import ChatRequest
        from server.routes import chat_stream as chat_stream_route

        async def scenario():
            req = ChatRequest(slug="s3", message="hi")
            with patch("server.routes._get_engine", return_value=engine):
                resp = await chat_stream_route(req, BackgroundTasks(), user_id=1)
                agen = resp.body_iterator
                await agen.__anext__()  # 消费第一个 SSE 事件后模拟断连
                await agen.aclose()

        asyncio.run(scenario())

        turns = _read_turns(env, "s3")
        assert len(turns) == 2, "回归：中断后已生成部分未落库"
        assert turns[0]["content"] == "hi"
        # 断连时第二、三段尚未生成，落库的正是已生成的那部分
        assert turns[1]["content"] == "第一段" * 10

    def test_error_mid_stream_persists_partial_reply(self, env):
        """生成中途异常：已生成的部分同样落库，且不影响错误事件下发。"""
        _make_exe(env, "s4")
        engine = MagicMock()

        def fake_stream(message, history):
            yield {"type": "text", "content": "开头"}
            raise RuntimeError("LLM 中断")

        engine.chat_stream.side_effect = fake_stream

        from server.models import ChatRequest
        from server.routes import chat_stream as chat_stream_route

        async def scenario():
            req = ChatRequest(slug="s4", message="hi")
            with patch("server.routes._get_engine", return_value=engine):
                resp = await chat_stream_route(req, BackgroundTasks(), user_id=1)
                chunks = []
                async for chunk in resp.body_iterator:
                    chunks.append(chunk)
                return chunks

        chunks = asyncio.run(scenario())
        assert any("error" in c for c in chunks)

        turns = _read_turns(env, "s4")
        assert len(turns) == 2
        assert turns[1]["content"] == "开头"

    def test_no_text_no_persist(self, env):
        """一个字都没生成就中断：不落空记录。"""
        _make_exe(env, "s5")
        engine = _mock_engine([{"type": "text", "content": ""}])

        from server.models import ChatRequest
        from server.routes import chat_stream as chat_stream_route

        async def scenario():
            req = ChatRequest(slug="s5", message="hi")
            with patch("server.routes._get_engine", return_value=engine):
                resp = await chat_stream_route(req, BackgroundTasks(), user_id=1)
                await resp.body_iterator.aclose()

        asyncio.run(scenario())
        assert _read_turns(env, "s5") == []


@pytest.mark.parametrize("stream", [False, True])
def test_incomplete_response_type_survives_safe_wrapper_without_provider_body(stream):
    from core.model_client import IncompleteModelResponse, _safe_stream, provider_call
    from core.retry import is_retryable

    error = IncompleteModelResponse()
    private_body = "synthetic-provider-secret-and-private-prompt"
    closed = []

    def fail():
        try:
            raise RuntimeError(private_body)
        except RuntimeError:
            raise error

    def chunks():
        try:
            yield "prefix"
            fail()
        finally:
            closed.append(True)

    with pytest.raises(IncompleteModelResponse) as caught:
        if stream:
            iterator = _safe_stream(chunks())
            assert next(iterator) == "prefix"
            next(iterator)
        else:
            provider_call(fail)
    assert caught.value is error
    assert not is_retryable(error)
    assert private_body not in "".join(traceback.format_exception(caught.value))
    if stream:
        assert closed == [True]


@pytest.fixture
def client_from_env(env):
    from server.app import create_app

    client = TestClient(create_app())
    headers = _login(client)
    return client, headers
