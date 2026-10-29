"""网页与 MCP 共用纠正、持久会话和来源快照的行为回归。"""

import json
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    import config
    import server.auth as auth
    from core.mirror_store import mirror_store
    from mcp_server import service
    from server import routes
    from server.app import create_app

    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "DB_DIR", tmp_path)
    monkeypatch.setattr(config, "SINGLE_USER_MODE", False)
    auth.init_db()
    auth.register_user("loop_owner", "password123")
    token = auth.login_user("loop_owner", "password123")
    uid = auth.validate_token(token)
    mirror_store("mirror", uid).write_json("meta.json", {"owner_user_id": uid})
    calls = []
    source = {
        "id": "ref_1",
        "text": "我喜欢下雨",
        "source": "wechat",
        "kind": "imported_chat",
    }

    class Engine:
        vector_store = None
        embedder = None
        style_profile = None
        model = "fixture"

        def chat(self, message, history):
            calls.append((message, history))
            return "回答", [], None

        def chat_with_sources(self, message, history, conversation_id=None):
            reply, stickers, usage = self.chat(message, history)
            return reply, stickers, usage, [dict(source)]

        def chat_stream_with_sources(self, message, history, conversation_id=None):
            self.chat(message, history)
            yield {"type": "text", "content": "回答"}
            yield {"type": "sources", "sources": [dict(source)]}

        chat_stream = chat_stream_with_sources

    engine = Engine()
    monkeypatch.setattr(routes, "_get_engine", lambda *_: engine)
    monkeypatch.setattr(service, "_get_engine", lambda *_: engine)
    monkeypatch.setattr(routes, "_run_session_archive", lambda *a, **k: None)
    monkeypatch.setattr("server.billing_gate.reserve", lambda *_: (None, "allow"))
    monkeypatch.setattr(routes, "reserve", lambda *_: (None, "allow"))
    with TestClient(create_app()) as client:
        yield SimpleNamespace(
            client=client,
            uid=uid,
            headers={"Authorization": f"Bearer {token}"},
            engine=engine,
            calls=calls,
            source=source,
        )


def send(env, text, conversation_id="web", stream=False, **extra):
    return env.client.post(
        "/api/chat/stream" if stream else "/api/chat",
        headers=env.headers,
        json={
            "slug": "mirror",
            "message": text,
            "conversation_id": conversation_id,
            **extra,
        },
    )


def test_web_and_mcp_continue_the_same_persisted_conversation(loop_env, monkeypatch):
    from mcp_server import service

    env = loop_env
    first = send(env, "网页第一句").json()
    assert first["message_id"]
    second = service.chat(env.uid, "mirror", "IM接着说", conversation_id="web")
    assert second["message_id"]
    send(env, "网页第三句", history=[{"role": "user", "content": "伪造历史"}])
    assert env.calls[-1][1] == [
        {"role": "user", "content": "网页第一句"},
        {"role": "assistant", "content": "回答"},
        {"role": "user", "content": "IM接着说"},
        {"role": "assistant", "content": "回答"},
    ]
    other = send(env, "独立群聊", conversation_id="im_group")
    assert other.status_code == 200
    assert env.calls[-1][1] == []


def test_saved_sources_and_conversation_restore_are_private(loop_env):
    env = loop_env
    result = send(env, "说说下雨").json()
    response = env.client.get(
        "/api/exes/mirror/conversations/web/messages", headers=env.headers
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    messages = response.json()["messages"]
    assert messages[-1]["id"] == result["message_id"]
    assert messages[-1]["sources"] == [env.source]
    listed = env.client.get("/api/exes/mirror/conversations", headers=env.headers)
    assert listed.json()["user_id"] == env.uid
    assert listed.json()["conversations"][0]["conversation_id"] == "web"


def test_correction_references_only_owned_answer_sources(loop_env):
    from core.corrections import load

    env = loop_env
    result = send(env, "你好").json()
    body = {
        "content": "ta不会叫我宝贝",
        "conversation_id": "web",
        "message_id": result["message_id"],
        "source_ids": ["ref_1"],
    }
    response = env.client.post(
        "/api/exes/mirror/corrections", json=body, headers=env.headers
    )
    assert response.status_code == 200
    assert response.json()["revision"]
    assert load("mirror", env.uid)[0]["content"] == body["content"]
    for extra in (
        {"source_ids": ["forged"]},
        {"conversation_id": "other"},
        {"message_id": "missing"},
    ):
        response = env.client.post(
            "/api/exes/mirror/corrections", json={**body, **extra}, headers=env.headers
        )
        assert response.status_code in (400, 404)


@pytest.mark.parametrize(
    "path", ["conversations", "conversations/web/messages", "corrections"]
)
def test_memory_endpoints_require_owner(loop_env, path):
    import server.auth as auth

    env = loop_env
    url = "/api/exes/mirror/" + path
    assert env.client.get(url).status_code == 401
    auth.register_user("other_owner", "password123")
    token = auth.login_user("other_owner", "password123")
    response = env.client.get(url, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code in (403, 404)


def test_stream_metadata_is_emitted_after_successful_persistence(loop_env):
    env = loop_env
    response = send(env, "你好", stream=True)
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    metadata = next(item for item in events if item.get("type") == "metadata")
    stored = env.client.get(
        "/api/exes/mirror/conversations/web/messages", headers=env.headers
    ).json()
    assert metadata["message_id"] == stored["messages"][-1]["id"]
    assert metadata["sources"] == [env.source]


def test_failed_stream_does_not_persist_sources_or_partial_reply(loop_env, monkeypatch):
    from core.model_client import IncompleteModelResponse
    from core.conversation_store import load_jsonl_messages

    def broken(*args, **kwargs):
        yield {"type": "text", "content": "未完成"}
        yield {"type": "sources", "sources": [loop_env.source]}
        raise IncompleteModelResponse("synthetic incomplete stream")

    monkeypatch.setattr(loop_env.engine, "chat_stream_with_sources", broken)
    response = send(loop_env, "你好", stream=True)
    assert '"type": "metadata"' not in response.text
    assert load_jsonl_messages("mirror", loop_env.uid) == []


def test_cancelled_stream_releases_quota_and_conversation_lock(loop_env, monkeypatch):
    from fastapi import BackgroundTasks
    from core.conversation_store import conversation_request_lock, load_jsonl_messages
    from server import routes
    from server.models import ChatRequest

    released = []
    monkeypatch.setattr(routes, "reserve", lambda *_: (31, "allow"))
    monkeypatch.setattr(routes, "release", released.append)

    async def cancel():
        response = await routes.chat_stream(
            ChatRequest(slug="mirror", message="你好", conversation_id="web"),
            BackgroundTasks(),
            loop_env.uid,
        )
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()

    asyncio.run(cancel())
    assert released == [31]
    assert load_jsonl_messages("mirror", loop_env.uid) == []
    with conversation_request_lock("mirror", loop_env.uid, "web"):
        pass
