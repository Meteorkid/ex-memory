"""存量镜像迁移：扁平布局 → 按账号隔离布局。"""

import json

import pytest


@pytest.fixture
def exes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("config.EXES_DIR", tmp_path)
    return tmp_path


def _make_flat(root, slug, owner=None, payload="内容"):
    d = root / slug
    d.mkdir(parents=True)
    meta = {"name": slug}
    if owner is not None:
        meta["owner_user_id"] = owner
    (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "SKILL.md").write_text(payload, encoding="utf-8")
    return d


class TestPlan:
    def test_owned_flat_exe_is_movable(self, exes_dir):
        from commands.migrate_layout import _plan

        _make_flat(exes_dir, "xiaoyu", owner=12)
        movable, manual = _plan()

        assert [(s, o) for s, o, _, _ in movable] == [("xiaoyu", 12)]
        assert manual == []

    def test_ownerless_exe_needs_human_decision(self, exes_dir):
        """归属未知时移动或删除都可能是错的，必须留给人判断。"""
        from commands.migrate_layout import _plan

        _make_flat(exes_dir, "test")
        movable, manual = _plan()

        assert movable == []
        assert manual[0][0] == "test"
        assert "owner_user_id" in manual[0][1]

    def test_existing_target_is_not_overwritten(self, exes_dir):
        from commands.migrate_layout import _plan

        _make_flat(exes_dir, "dup", owner=5)
        (exes_dir / "5" / "dup").mkdir(parents=True)

        movable, manual = _plan()
        assert movable == []
        assert "已存在" in manual[0][1]

    def test_already_nested_exe_is_skipped(self, exes_dir):
        from commands.migrate_layout import _plan

        nested = exes_dir / "9" / "done"
        nested.mkdir(parents=True)
        (nested / "meta.json").write_text(
            json.dumps({"owner_user_id": 9}), encoding="utf-8"
        )

        movable, manual = _plan()
        assert movable == []
        assert manual == []


class TestApply:
    def test_dry_run_moves_nothing(self, exes_dir, capsys):
        from commands.migrate_layout import cmd_migrate_layout

        src = _make_flat(exes_dir, "xiaoyu", owner=12)
        cmd_migrate_layout("")

        assert src.exists()
        assert not (exes_dir / "12" / "xiaoyu").exists()
        assert "未改动任何文件" in capsys.readouterr().out

    def test_apply_moves_and_preserves_content(self, exes_dir):
        from commands.migrate_layout import cmd_migrate_layout

        src = _make_flat(exes_dir, "xiaoyu", owner=12, payload="ta 的人格")
        cmd_migrate_layout("--apply")

        target = exes_dir / "12" / "xiaoyu"
        assert not src.exists()
        assert target.exists()
        assert (target / "SKILL.md").read_text(encoding="utf-8") == "ta 的人格"

    def test_apply_leaves_ownerless_untouched(self, exes_dir):
        from commands.migrate_layout import cmd_migrate_layout

        orphan = _make_flat(exes_dir, "test")
        cmd_migrate_layout("--apply")

        assert orphan.exists()
        assert (orphan / "meta.json").exists()

    def test_migrated_exe_is_resolvable_by_owner(self, exes_dir):
        """迁移后必须能按 owner 解析到，否则等于把镜像弄丢了。"""
        from commands.migrate_layout import cmd_migrate_layout
        from config import resolve_ex_dir

        _make_flat(exes_dir, "xiaoyu", owner=12)
        cmd_migrate_layout("--apply")

        assert resolve_ex_dir("xiaoyu", 12) == exes_dir / "12" / "xiaoyu"
