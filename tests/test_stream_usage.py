"""D-05 回归：流式路径 token 计量。

- 端点支持 stream_options 时取真实 usage
- 不支持时回退估算，且估算必须包含 system prompt（此前完全漏算）
- 路由层把 usage 计入 session 计数器，口径与 /chat 一致
"""

import json
import tempfile
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

# SKILL.md 填充到 3000 个汉字：估算约 2000 tokens，
# 若流式估算漏算 system prompt，prompt_tokens 只会是个位数
_BIG_SKILL = "# 人格档案\n" + "细节描述" * 750


@contextmanager
def stub_llm():
    """把 LLM 客户端打桩，并重置路由让它重新取配置。

    引擎不再自持客户端（由 core.llm_router 统一持有），所以打桩范围必须
    覆盖到「调用时刻」，不能只覆盖构造。
    """
    from core import llm_router

    llm_router.reset_for_tests()
    with patch("config.get_llm_client") as factory:
        client = MagicMock()
        factory.return_value = client
        yield client
    llm_router.reset_for_tests()


def _make_engine(tmpdir, skill_text=_BIG_SKILL):
    """构造真实 ChatEngine，client 打桩。"""
    from pathlib import Path

    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    (tmpdir / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (tmpdir / "sessions").mkdir(exist_ok=True)

    with (
        patch("core.engine.resolve_ex_dir", return_value=tmpdir),
        patch(
            "core.engine.get_llm_config",
            return_value={
                "model": "test",
                "temperature": 0.8,
                "top_p": 0.9,
                "frequency_penalty": 0.6,
                "max_tokens": 4096,
            },
        ),
    ):
        from core.engine import ChatEngine

        return ChatEngine("test", vector_store=None, embedder=None)


def _text_chunk(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content))],
        usage=None,
    )


def _usage_chunk(prompt_tokens, completion_tokens):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )


class TestEngineStreamUsage:
    def test_stream_options_requested(self):
        """_call_stream 必须带 stream_options include_usage。"""
        with tempfile.TemporaryDirectory() as tmpdir, stub_llm() as llm_client:
            engine = _make_engine(tmpdir)
            llm_client.chat.completions.create.return_value = iter(
                [_text_chunk("好"), _usage_chunk(1, 1)]
            )
            list(engine.chat_stream("hi", []))

            kwargs = llm_client.chat.completions.create.call_args.kwargs
            assert kwargs["stream_options"] == {"include_usage": True}

    def test_real_usage_passthrough(self):
        """带 usage 的最终 chunk → usage 事件透出真实值。"""
        with tempfile.TemporaryDirectory() as tmpdir, stub_llm() as llm_client:
            engine = _make_engine(tmpdir)
            llm_client.chat.completions.create.return_value = iter(
                [_text_chunk("你好"), _usage_chunk(5691, 12)]
            )

            events = list(engine.chat_stream("hi", []))

            usage_events = [e for e in events if e["type"] == "usage"]
            assert usage_events == [
                {"type": "usage", "prompt_tokens": 5691, "completion_tokens": 12}
            ]

    def test_fallback_estimate_includes_system_prompt(self):
        """端点不支持 stream_options（流中无 usage）→ 回退估算必须计入 system prompt。"""
        from core.validation import estimate_tokens

        with tempfile.TemporaryDirectory() as tmpdir, stub_llm() as llm_client:
            engine = _make_engine(tmpdir)
            llm_client.chat.completions.create.return_value = iter(
                [_text_chunk("嗯嗯")]
            )

            events = list(engine.chat_stream("hi", []))

            usage_events = [e for e in events if e["type"] == "usage"]
            assert len(usage_events) == 1
            # SKILL.md 自身估算约 2000 tokens；修复前估算只含 "hi"（≈1 token）
            skill_tokens = estimate_tokens(_BIG_SKILL)
            assert usage_events[0]["prompt_tokens"] > skill_tokens, (
                "回归：流式估算漏算了 system prompt"
            )
            assert usage_events[0]["completion_tokens"] == estimate_tokens("嗯嗯")


class TestRouteCountsStreamUsage:
    """路由层把 usage 事件计入 session 计数器。"""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
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
        with routes._counter_lock:
            routes._session_counters.clear()
        routes._engine_cache.clear()
        yield exes
        with routes._counter_lock:
            routes._session_counters.clear()
        routes._engine_cache.clear()

    def test_usage_event_updates_session_counter(self, env):
        _make_route_exe(env, "u1")
        from server.app import create_app

        client = TestClient(create_app())
        client.post(
            "/api/auth/register", json={"username": "uc", "password": "pass1234"}
        )
        token = client.post(
            "/api/auth/login", json={"username": "uc", "password": "pass1234"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        engine = MagicMock()

        def fake_stream(message, history):
            yield {"type": "text", "content": "好的"}
            yield {"type": "usage", "prompt_tokens": 5691, "completion_tokens": 12}

        engine.chat_stream.side_effect = fake_stream

        with patch("server.routes._get_engine", return_value=engine):
            resp = client.post(
                "/api/chat/stream",
                json={"slug": "u1", "message": "hi"},
                headers=headers,
            )
        assert resp.status_code == 200
        # usage 事件随流下发（前端忽略未知类型）
        assert "5691" in resp.text

        usage = client.get("/api/exes/u1/usage", headers=headers).json()
        assert usage["prompt_tokens"] == 5691
        assert usage["completion_tokens"] == 12
        assert usage["turns"] == 1


def _make_route_exe(exes_dir, slug: str, owner: int = 1):
    ex_dir = exes_dir / slug
    ex_dir.mkdir(parents=True, exist_ok=True)
    (ex_dir / "meta.json").write_text(
        json.dumps({"name": slug, "slug": slug, "owner_user_id": owner}),
        encoding="utf-8",
    )
