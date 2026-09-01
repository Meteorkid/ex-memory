"""D-01 回归：跨用户引擎缓存隔离。

复现路径：用户 A 创建镜像并对话 → 删除 → 用户 B 创建同名镜像 → 对话。
修复前 _engine_cache 仅以 slug 为键，删除/创建也不触发失效，
B 会命中 A 的旧引擎，读到 A 的人格档案（skill_content）。
"""

import json
import shutil
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    """独立用户库 + 独立 exes 目录，引擎缓存用例间隔离。"""
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

    routes._engine_cache.clear()
    monkeypatch.setattr(routes, "_login_limiter", None)
    yield exes
    routes._engine_cache.clear()


@pytest.fixture
def client(env):
    from server.app import create_app

    return TestClient(create_app())


def _login(client, username, password="pass1234"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    return r.json()["token"]


def _make_exe(exes_dir, slug: str, owner: int, skill_text: str):
    """落一个最小可用镜像：SKILL.md + 带 owner 的 meta.json。"""
    ex_dir = exes_dir / slug
    ex_dir.mkdir(parents=True, exist_ok=True)
    (ex_dir / "SKILL.md").write_text(skill_text, encoding="utf-8")
    (ex_dir / "meta.json").write_text(
        json.dumps(
            {
                "name": slug,
                "slug": slug,
                "owner_user_id": owner,
                "created_at": "2024-01-01T00:00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class TestDeleteInvalidatesCache:
    """删除镜像必须清缓存，否则同名重建后返回上一任 owner 的引擎。"""

    def test_recreated_exe_does_not_serve_previous_owner_engine(self, env, client):
        import server.routes as routes

        slug = "shared"
        token_a = _login(client, "alice")

        # A 创建镜像并对话一次 → 引擎（含 A 的人格档案）进入缓存
        _make_exe(env, slug, owner=1, skill_text="# A 的前任画像：内向敏感")
        engine_a = routes._get_engine(slug, 1)
        assert "A 的前任画像" in engine_a.skill_content

        # A 删除镜像（走真实路由，验证 delete 触发缓存失效）
        resp = client.request(
            "DELETE",
            f"/api/exes/{slug}",
            json={"confirm": True},
            headers=_auth(token_a),
        )
        assert resp.status_code == 200
        assert not (env / slug).exists()

        # B 创建同名镜像（直接落盘，等价于 create 流程完成后的状态）
        _make_exe(env, slug, owner=2, skill_text="# B 的前任画像：外向开朗")

        engine_b = routes._get_engine(slug, 2)
        assert engine_b is not engine_a, "回归：B 命中了 A 的旧引擎实例"
        assert "B 的前任画像" in engine_b.skill_content, "回归：B 读到了 A 的人格档案"


class TestCreateInvalidatesStaleCache:
    """创建镜像必须清掉同名旧缓存（目录可能已被外部删除而缓存残留）。"""

    def test_create_exe_purges_stale_entry(self, env, client):
        import server.routes as routes

        slug = "ghost"
        token_b = _login(client, "bob")  # user_id=1，这里同时充当"新 owner"

        _make_exe(env, slug, owner=1, skill_text="# 旧画像")
        engine_old = routes._get_engine(slug, 1)

        # 模拟目录被删除但缓存残留（外部删除/旧版本 bug）
        shutil.rmtree(env / slug)

        def fake_create_flow(**kwargs):
            _make_exe(env, slug, owner=1, skill_text="# 新画像")
            return {"state": "completed", "slug": slug}

        with patch(
            "pipeline.orchestrator.run_create_flow_api", side_effect=fake_create_flow
        ):
            resp = client.post(
                "/api/exes",
                json={"name": "B", "slug": slug},
                headers=_auth(token_b),
            )
            assert resp.status_code == 200

        engine_new = routes._get_engine(slug, 1)
        assert engine_new is not engine_old, "回归：创建后仍命中旧引擎"
        assert "新画像" in engine_new.skill_content


class TestResumeInvalidatesCache:
    """恢复创建会重写 SKILL.md，完成后必须刷新缓存。"""

    def test_resume_exe_reloads_skill(self, env, client):
        import server.routes as routes

        slug = "wip"
        token_a = _login(client, "carol")

        _make_exe(env, slug, owner=1, skill_text="# 半成品画像")
        engine_old = routes._get_engine(slug, 1)
        assert "半成品画像" in engine_old.skill_content

        def fake_resume_flow(**kwargs):
            (env / slug / "SKILL.md").write_text("# 完整画像", encoding="utf-8")
            return {"state": "completed", "slug": slug}

        with patch(
            "pipeline.orchestrator.run_create_flow_api",
            side_effect=fake_resume_flow,
        ):
            resp = client.post(
                f"/api/exes/{slug}/resume",
                json={"name": "C", "slug": slug},
                headers=_auth(token_a),
            )
            assert resp.status_code == 200

        engine_new = routes._get_engine(slug, 1)
        assert engine_new is not engine_old, "回归：resume 后仍命中旧引擎"
        assert "完整画像" in engine_new.skill_content


class TestCacheKeyIncludesUser:
    """缓存键必须包含用户维度：同名镜像下不同用户的引擎互不串用。"""

    def test_same_slug_different_users_get_isolated_engines(self, env, client):
        import server.routes as routes

        slug = "twins"
        _login(client, "dave")  # user_id=1
        _login(client, "erin")  # user_id=2

        _make_exe(env, slug, owner=1, skill_text="# 唯一画像")
        engine_u1 = routes._get_engine(slug, 1)
        engine_u2 = routes._get_engine(slug, 2)
        assert engine_u1 is not engine_u2

        # 按 slug 失效时必须清掉所有用户的条目
        routes._invalidate_engine(slug)
        assert not any(k[1] == slug for k in routes._engine_cache)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
