"""D-03 回归：slug 命名空间隔离。

缺陷背景：镜像此前是全局扁平目录 exes/<slug>，不同用户 id 不同的同名
镜像会互相覆盖，且用一个全局 slug 即可探知他人镜像存在。

修复后：
- 新建镜像落位 exes/<owner>/<slug>（嵌套），同名 slug 按账号隔离；
- 存量镜像仍留在 exes/<slug>（扁平），通过 resolve_ex_dir 回退兼容；
- 未指定 owner（单用户 CLI / Gradio）退化到扁平目录，不误建 exes/None/。
"""

import json

import pytest


@pytest.fixture
def exes_dir(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "EXES_DIR", tmp_path)
    return tmp_path


# ── resolve_ex_dir 行为 ───────────────────────────────


def test_resolve_ex_dir_prioritizes_nested_then_falls_back_flat(exes_dir):
    from config import resolve_ex_dir, get_ex_dir_owned

    slug = "s1"
    owner = 42
    nested = get_ex_dir_owned(slug, owner)
    nested.mkdir(parents=True)

    # 嵌套存在 → 命中嵌套
    assert resolve_ex_dir(slug, owner) == nested

    # 嵌套不存在但有扁平存量 → 回退扁平
    legacy = exes_dir / "legacy"
    legacy.mkdir()
    assert resolve_ex_dir("legacy", owner) == legacy


def test_resolve_ex_dir_without_owner_uses_flat(exes_dir):
    from config import resolve_ex_dir, ensure_ex_dirs

    ex_dir = ensure_ex_dirs("legacy")
    assert ex_dir.exists()
    # owner 为空时无法定位嵌套目录，直接落在扁平目录
    assert resolve_ex_dir("legacy") == exes_dir / "legacy"


def test_ensure_ex_dirs_owned_none_falls_back_flat(exes_dir):
    """owner=None 必须退化为扁平目录，禁止生成 exes/None/<slug>。"""
    from config import ensure_ex_dirs_owned

    ex_dir = ensure_ex_dirs_owned("noowner", None)
    assert ex_dir == exes_dir / "noowner"
    assert not (exes_dir / "None").exists()


def test_ensure_ex_dirs_owned_creates_owned_skeleton(exes_dir):
    from config import ensure_ex_dirs_owned

    ex_dir = ensure_ex_dirs_owned("nest", 7)
    assert ex_dir == exes_dir / "7" / "nest"
    for sub in ("chroma_db", "sessions", "versions"):
        assert (ex_dir / sub).is_dir()


# ── conversation_store 按 owner 隔离 ──────────────────


class TestConversationStoreNamespace:
    def test_same_slug_different_owners_isolated(self, exes_dir):
        from core.conversation_store import append_turn, load_jsonl_messages

        append_turn("shared", 1, "a的问题", "a的回答")
        append_turn("shared", 2, "b的问题", "b的回答")

        a = load_jsonl_messages("shared", owner=1)
        b = load_jsonl_messages("shared", owner=2)
        assert [m["content"] for m in a] == ["a的问题", "a的回答"]
        assert [m["content"] for m in b] == ["b的问题", "b的回答"]

        # 目录物理隔离：exes/1/shared 与 exes/2/shared
        assert (exes_dir / "1" / "shared" / "conversations").is_dir()
        assert (exes_dir / "2" / "shared" / "conversations").is_dir()


# ── exe_access 嵌套与扁平兼容 ──────────────────────────


class TestExeAccessNamespace:
    def test_iter_accessible_exes_covers_nested_and_flat(self, exes_dir):
        from config import ensure_ex_dirs_owned, ensure_ex_dirs
        from core.exe_access import set_owner_user_id, iter_accessible_exes

        # 嵌套镜像
        nested = ensure_ex_dirs_owned("nest", 1) / "meta.json"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text(
            json.dumps({"name": "nest", "slug": "nest", "owner_user_id": 1}),
            encoding="utf-8",
        )

        # 扁平镜像（绑定 owner）
        flat = ensure_ex_dirs("flat-legacy")
        (flat / "meta.json").write_text(
            json.dumps({"name": "flat-legacy", "slug": "flat-legacy"}), encoding="utf-8"
        )
        set_owner_user_id("flat-legacy", 1)

        dirs = list(iter_accessible_exes(1))
        names = {d.name for d in dirs}
        assert "nest" in names
        assert "flat-legacy" in names

    def test_same_slug_two_owners_isolated(self, exes_dir, monkeypatch):
        """新旧两个用户各自新建同名 'shared'：落位不同嵌套目录，互不读取。"""
        from config import ensure_ex_dirs_owned
        from config import resolve_ex_dir
        from core.exe_access import load_meta

        monkeypatch.setattr("config.SINGLE_USER_MODE", False)

        d3 = ensure_ex_dirs_owned("shared", 3)
        d9 = ensure_ex_dirs_owned("shared", 9)
        (d3 / "meta.json").write_text(
            json.dumps({"name": "shared", "owner_user_id": 3}), encoding="utf-8"
        )
        (d9 / "meta.json").write_text(
            json.dumps({"name": "shared", "owner_user_id": 9}), encoding="utf-8"
        )

        # 各自解析到各自的命名空间，内容互不串
        assert resolve_ex_dir("shared", 3) == d3
        assert resolve_ex_dir("shared", 9) == d9
        assert load_meta("shared", 3)["owner_user_id"] == 3
        assert load_meta("shared", 9)["owner_user_id"] == 9
