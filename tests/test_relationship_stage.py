"""FR-005 回归：关系阶段接线（此前 STAGE_INSTRUCTIONS 是死代码）。"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


def _make_engine(tmpdir, meta_extra: dict | None = None):
    """构造真实 ChatEngine（client 打桩）。"""
    from core.engine import ChatEngine

    tmpdir = Path(tmpdir)
    tmpdir.mkdir(parents=True, exist_ok=True)
    (tmpdir / "SKILL.md").write_text("# 测试人格", encoding="utf-8")
    (tmpdir / "sessions").mkdir(exist_ok=True)
    (tmpdir / "meta.json").write_text(
        json.dumps(meta_extra or {}, ensure_ascii=False), encoding="utf-8"
    )

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
        patch("core.engine.get_llm_client") as mock_client,
    ):
        mock_client.return_value = MagicMock()
        return ChatEngine("test", vector_store=None, embedder=None)


class TestStageInjectedIntoPrompt:
    """四个阶段各生成一次 system prompt，断言含对应指令文本。"""

    @pytest.mark.parametrize(
        "stage,keyword",
        [
            ("dating", "热恋期"),
            ("conflicted", "磨合期"),
            ("broken", "分手期"),
            ("healing", "治愈期"),
        ],
    )
    def test_prompt_contains_stage_instruction(self, stage, keyword):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, {"stage": stage})
            prompt = engine._build_system_prompt()
            assert keyword in prompt
            assert "当前关系阶段" in prompt

    def test_reads_web_key_and_cli_key(self):
        """Web 写 "stage"、CLI 写 "relationship_stage"，两个键都生效。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, {"relationship_stage": "broken"})
            assert engine.relationship_stage == "broken"
            assert "分手期" in engine._build_system_prompt()

    def test_web_key_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(
                tmpdir, {"stage": "healing", "relationship_stage": "broken"}
            )
            assert engine.relationship_stage == "healing"

    def test_unknown_stage_falls_back_to_dating(self):
        """meta 里的未知阶段值不能引发 KeyError。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, {"stage": "situationship"})
            assert engine.relationship_stage == "dating"
            prompt = engine._build_system_prompt()
            assert "热恋期" in prompt

    def test_default_is_dating_without_meta_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir, {})
            assert engine.relationship_stage == "dating"
            assert "热恋期" in engine._build_system_prompt()


class TestPutStageInvalidatesEngineCache:
    """PUT /exes/{slug}/stage 后，缓存引擎必须带上新阶段。"""

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
        with routes._engine_cache_lock:
            routes._engine_cache.clear()
        yield exes
        with routes._engine_cache_lock:
            routes._engine_cache.clear()

    def test_engine_reloads_stage_after_put(self, env):
        import server.routes as routes
        from server.app import create_app

        slug = "staged"
        ex_dir = env / slug
        ex_dir.mkdir()
        (ex_dir / "SKILL.md").write_text("# 画像", encoding="utf-8")
        (ex_dir / "meta.json").write_text(
            json.dumps({"name": slug, "slug": slug, "owner_user_id": 1}),
            encoding="utf-8",
        )

        client = TestClient(create_app())
        client.post(
            "/api/auth/register", json={"username": "sc", "password": "pass1234"}
        )
        token = client.post(
            "/api/auth/login", json={"username": "sc", "password": "pass1234"}
        ).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        engine_before = routes._get_engine(slug, 1)
        assert engine_before.relationship_stage == "dating"

        resp = client.put("/api/exes/staged/stage?stage=broken", headers=headers)
        assert resp.status_code == 200

        engine_after = routes._get_engine(slug, 1)
        assert engine_after is not engine_before, "回归：切阶段后缓存未失效"
        assert engine_after.relationship_stage == "broken"
        assert "分手期" in engine_after._build_system_prompt()
