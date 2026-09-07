"""FR-006 回归：会话摘要与人格更新接入 Web（此前仅 CLI 生效）。"""

import json
import threading
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from config import ARCHIVE_THRESHOLD


@pytest.fixture
def env(tmp_path, monkeypatch):
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
    routes._engine_cache.clear()
    yield exes
    routes._engine_cache.clear()


def _make_exe(exes_dir, slug: str, owner: int = 1):
    ex_dir = exes_dir / slug
    ex_dir.mkdir(parents=True, exist_ok=True)
    (ex_dir / "SKILL.md").write_text(
        "# PART A 画像\n\n---\n\n## PART B\n其他", encoding="utf-8"
    )
    (ex_dir / "meta.json").write_text(
        json.dumps({"name": slug, "slug": slug, "owner_user_id": owner}),
        encoding="utf-8",
    )
    return ex_dir


def _mock_llm(summary_text: str):
    """打桩摘要 LLM 调用（防止测试触网）。

    config 必须给全字段：patch 期间 core.engine 可能被首次惰性导入，
    若绑定到缺字段的 mock 配置，后续真实 ChatEngine 构造会 KeyError。
    """
    response = MagicMock()
    response.choices = [MagicMock(message=MagicMock(content=summary_text))]
    client = MagicMock()
    client.chat.completions.create.return_value = response
    full_cfg = {
        "api_key": "k",
        "base_url": "http://test",
        "model": "m",
        "temperature": 0.3,
        "top_p": 0.9,
        "frequency_penalty": 0.6,
        "max_tokens": 4096,
    }
    return (
        patch("config.get_llm_config", return_value=full_cfg),
        patch("config.get_llm_client", return_value=client),
    )


def _raw_archives(sessions_dir):
    """原始归档文件（排除 *_summary.md，两者都匹配 session_*.md）。"""
    return [
        p
        for p in sessions_dir.glob("session_*.md")
        if not p.name.endswith("_summary.md")
    ]


def _add_turns(slug: str, n: int, exes_dir):
    from core.conversation_store import append_turn

    for i in range(n):
        append_turn(slug, 1, f"问{i}", f"答{i}", source="web")


class TestMaybeArchive:
    def test_below_threshold_noop(self, env):
        from core.session_archive import maybe_archive

        _make_exe(env, "a1")
        _add_turns("a1", ARCHIVE_THRESHOLD - 1, env)

        cfg_p, client_p = _mock_llm("不应被调用")
        with cfg_p, client_p:
            assert maybe_archive("a1") is False
        assert not list((env / "a1" / "sessions").glob("session_*"))

    def test_threshold_archives_and_summarizes(self, env):
        from core.session_archive import maybe_archive

        _make_exe(env, "a2")
        _add_turns("a2", ARCHIVE_THRESHOLD, env)

        cfg_p, client_p = _mock_llm("这是会话摘要：聊得很开心")
        with cfg_p, client_p:
            assert maybe_archive("a2") is True

        sessions = env / "a2" / "sessions"
        raw = _raw_archives(sessions)
        summaries = list(sessions.glob("*_summary.md"))
        assert len(raw) == 1 and len(summaries) == 1
        assert summaries[0].read_text(encoding="utf-8") == "这是会话摘要：聊得很开心"
        raw_text = raw[0].read_text(encoding="utf-8")
        assert "**用户**: 问0" in raw_text

        # SKILL.md 记忆段更新（人格随对话成长）
        skill = (env / "a2" / "SKILL.md").read_text(encoding="utf-8")
        assert "### 对话摘要" in skill
        assert "这是会话摘要：聊得很开心" in skill
        assert "## PART B" in skill  # 原 marker 保留

        # 归档后状态推进：再调一次不会重复归档
        cfg_p2, client_p2 = _mock_llm("不应生成第二份")
        with cfg_p2, client_p2:
            assert maybe_archive("a2") is False
        assert len(list(sessions.glob("*_summary.md"))) == 1

    def test_llm_failure_degrades_to_raw_archive(self, env):
        from core.session_archive import maybe_archive

        _make_exe(env, "a3")
        _add_turns("a3", ARCHIVE_THRESHOLD, env)

        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content=None))]
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("LLM 挂了")
        with (
            patch("config.get_llm_config", return_value={"api_key": "k", "model": "m"}),
            patch("config.get_llm_client", return_value=client),
        ):
            # 摘要失败不抛异常：降级为只有原始归档
            assert maybe_archive("a3") is True

        sessions = env / "a3" / "sessions"
        assert len(_raw_archives(sessions)) == 1
        assert len(list(sessions.glob("*_summary.md"))) == 0

    def test_concurrent_calls_archive_once(self, env):
        """两个请求同时触发归档：认领锁保证只归档一次。"""
        from core.session_archive import maybe_archive

        _make_exe(env, "a4")
        _add_turns("a4", ARCHIVE_THRESHOLD, env)

        cfg_p, client_p = _mock_llm("并发摘要")
        results = []
        with cfg_p, client_p:

            def worker():
                results.append(maybe_archive("a4"))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert results.count(True) == 1, "回归：并发触发了重复归档"
        assert len(list((env / "a4" / "sessions").glob("*_summary.md"))) == 1


class TestArchiveSessionUnit:
    def test_archive_session_updates_engine_summaries(self, env):
        """CLI 路径：engine.session_summaries 即时更新。"""
        from core.session_archive import archive_session

        _make_exe(env, "a5")
        engine = MagicMock()
        engine.session_summaries = []

        cfg_p, client_p = _mock_llm("CLI 摘要")
        with cfg_p, client_p:
            result = archive_session(
                "a5",
                [
                    {"role": "user", "content": "在吗"},
                    {"role": "assistant", "content": "在"},
                ],
                engine=engine,
            )

        assert result is not None
        assert result["summary"] == "CLI 摘要"
        assert engine.session_summaries == ["CLI 摘要"]

    def test_archive_session_empty_messages(self, env):
        from core.session_archive import archive_session

        _make_exe(env, "a6")
        assert archive_session("a6", []) is None


class TestWebPathIntegration:
    def test_stream_chat_reaches_threshold_and_feeds_next_prompt(self, env):
        """Web 路径：满阈值轮次后 sessions/ 产生摘要，下一轮 prompt 含记忆层。"""
        import server.routes as routes
        from server.app import create_app

        slug = "w1"
        _make_exe(env, slug)
        client = TestClient(create_app())
        client.post(
            "/api/auth/register", json={"username": "web", "password": "pass1234"}
        )
        token = client.post(
            "/api/auth/login", json={"username": "web", "password": "pass1234"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        from server.auth import validate_token

        token_uid = validate_token(token)

        engine = MagicMock()

        def fake_stream(message, history):
            yield {"type": "text", "content": "回复"}
            yield {"type": "usage", "prompt_tokens": 1, "completion_tokens": 1}

        engine.chat_stream.side_effect = fake_stream

        # 按量计费下，初始体验余额只够 10 轮（¥0.20 / ¥0.02 每轮），
        # 而归档需要满 ARCHIVE_THRESHOLD 轮。先充值补齐，否则中途会被余额闸门拦下。
        from core.billing import ensure_account, topup
        from config import TURN_PRICE_MICROS

        account_id = ensure_account(token_uid)
        topup(account_id, ARCHIVE_THRESHOLD * int(TURN_PRICE_MICROS))

        cfg_p, client_p = _mock_llm("记忆层摘要：我们聊了很多")
        with cfg_p, client_p, patch("server.routes._get_engine", return_value=engine):
            for i in range(ARCHIVE_THRESHOLD):
                resp = client.post(
                    "/api/chat/stream",
                    json={"slug": slug, "message": f"消息{i}"},
                    headers=headers,
                )
                assert resp.status_code == 200

        summaries = list((env / slug / "sessions").glob("*_summary.md"))
        assert len(summaries) == 1, "回归：Web 路径满阈值后未生成会话摘要"

        # 归档触发了缓存失效 → 新引擎带上记忆层
        fresh_engine = routes._get_engine(slug, 1)
        prompt = fresh_engine._build_system_prompt()
        assert "最近对话记忆" in prompt, "回归：记忆层未注入 system prompt"
        assert "记忆层摘要：我们聊了很多" in prompt
