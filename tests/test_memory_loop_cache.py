"""共享文件变更必须让 Web/MCP 各自的热缓存读取新记忆。"""

from types import SimpleNamespace

import pytest


@pytest.fixture
def cached_mirror(tmp_path, monkeypatch):
    import config
    import server.auth as auth
    from core.corrections import prompt_section
    from core.mirror_store import mirror_store
    from mcp_server import service
    from server import routes

    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "DB_DIR", tmp_path)
    monkeypatch.setattr(config, "SINGLE_USER_MODE", False)
    auth.init_db()
    uid = auth.get_or_create_external_user_id("test", "cache-owner")
    store = mirror_store("mirror", uid)
    store.write_json("meta.json", {"owner_user_id": uid})
    store.write_text("SKILL.md", "人物资料")
    settings = {"revision": 1}
    monkeypatch.setattr(
        "server.model_settings.get_settings_revision", lambda _: settings["revision"]
    )

    def factory(slug, owner):
        engine = SimpleNamespace(
            corrections=prompt_section(slug, owner), skill=store.read_text("SKILL.md")
        )
        return engine, None, None

    monkeypatch.setattr("core.factory.create_engine_and_store", factory)
    service.invalidate_engine()
    routes._evict_engines_for_slug("mirror")
    yield uid, store, settings
    service.invalidate_engine()
    routes._evict_engines_for_slug("mirror")


def test_both_hot_caches_observe_correction_without_local_broadcast(cached_mirror):
    from core.corrections import add
    from mcp_server import service
    from server import routes

    uid, _store, _settings = cached_mirror
    web_before = routes._get_engine("mirror", uid)
    mcp_before = service._get_engine("mirror", uid)
    assert routes._get_engine("mirror", uid) is web_before
    assert service._get_engine("mirror", uid) is mcp_before
    add("mirror", "请叫我小雨，不要叫宝贝", uid)
    web_after = routes._get_engine("mirror", uid)
    mcp_after = service._get_engine("mirror", uid)
    assert web_after is not web_before and mcp_after is not mcp_before
    assert "请叫我小雨" in web_after.corrections
    assert "请叫我小雨" in mcp_after.corrections


def test_material_update_and_model_revision_refresh_both_caches(cached_mirror):
    from mcp_server import service
    from server import routes

    uid, store, settings = cached_mirror
    initial = [routes._get_engine("mirror", uid), service._get_engine("mirror", uid)]
    store.write_text("SKILL.md", "修订后人物资料")
    updated = [routes._get_engine("mirror", uid), service._get_engine("mirror", uid)]
    assert all(
        new is not old and "修订后" in new.skill for new, old in zip(updated, initial)
    )
    settings["revision"] += 1
    final = [routes._get_engine("mirror", uid), service._get_engine("mirror", uid)]
    assert all(new is not old for new, old in zip(final, updated))


def test_recreated_mirror_cannot_reuse_an_old_engine(cached_mirror):
    import shutil
    from core.memory_state import get_memory_revision
    from mcp_server import service
    from server import routes

    uid, store, _settings = cached_mirror
    old = [routes._get_engine("mirror", uid), service._get_engine("mirror", uid)]
    revision = get_memory_revision("mirror", uid)
    shutil.rmtree(store.base_dir)
    store.write_json("meta.json", {"owner_user_id": uid})
    store.write_text("SKILL.md", "重建镜像资料")
    assert get_memory_revision("mirror", uid) == revision
    new = [routes._get_engine("mirror", uid), service._get_engine("mirror", uid)]
    assert all(
        after is not before and after.skill == "重建镜像资料"
        for before, after in zip(old, new)
    )
