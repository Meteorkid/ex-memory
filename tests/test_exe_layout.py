"""镜像目录布局：扁平（exes/<slug>）与嵌套（exes/<owner>/<slug>）的遍历与解析。

D-03 把新建镜像改为按账号隔离后，平台级任务如果还用 EXES_DIR.iterdir()，
会把 owner 目录当成镜像本身，从而漏掉其下所有真实镜像——过期清理正是如此。
"""

import json
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def exes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("config.EXES_DIR", tmp_path)
    return tmp_path


def _make_exe(root, *parts, meta=True):
    """在 root 下建一个镜像目录，parts 为相对路径段。"""
    d = root.joinpath(*parts)
    d.mkdir(parents=True)
    if meta:
        (d / "meta.json").write_text(json.dumps({"name": parts[-1]}), encoding="utf-8")
    return d


def _write_expired_conversation(ex_dir, days_ago=40):
    conv = ex_dir / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat()
    (conv / "conversation.jsonl").write_text(
        json.dumps({"role": "user", "content": "过期", "created_at": ts}) + "\n",
        encoding="utf-8",
    )
    return conv / "conversation.jsonl"


class TestIterExeDirs:
    def test_yields_both_layouts(self, exes_dir):
        from config import iter_exe_dirs

        _make_exe(exes_dir, "legacy")
        _make_exe(exes_dir, "12", "nested")

        found = {(slug, owner) for slug, owner, _ in iter_exe_dirs()}
        assert ("legacy", None) in found
        assert ("nested", 12) in found

    def test_digit_named_flat_exe_is_not_mistaken_for_owner_dir(self, exes_dir):
        """存量镜像可以叫 "1"；带 meta.json 时必须按扁平镜像处理，而不是账号命名空间。"""
        from config import iter_exe_dirs

        _make_exe(exes_dir, "1")

        found = {(slug, owner) for slug, owner, _ in iter_exe_dirs()}
        assert found == {("1", None)}

    def test_require_meta_false_includes_broken_exe(self, exes_dir):
        """meta.json 缺失的残缺镜像，合规类任务仍要覆盖到——漏删比多删风险大。"""
        from config import iter_exe_dirs

        _make_exe(exes_dir, "broken", meta=False)

        assert ("broken", None) not in {(s, o) for s, o, _ in iter_exe_dirs()}
        lenient = {(s, o) for s, o, _ in iter_exe_dirs(require_meta=False)}
        assert ("broken", None) in lenient


class TestFindExDir:
    def test_prefers_flat_then_falls_back_to_nested(self, exes_dir):
        from config import find_ex_dir

        nested = _make_exe(exes_dir, "7", "solo")
        assert find_ex_dir("solo") == nested

        flat = _make_exe(exes_dir, "solo")
        assert find_ex_dir("solo") == flat

    def test_returns_none_when_missing(self, exes_dir):
        from config import find_ex_dir

        assert find_ex_dir("nope") is None

    def test_raises_on_cross_account_ambiguity(self, exes_dir):
        """CLI 没有身份上下文，同名镜像必须报错而不是随便选一个别人的。"""
        from config import find_ex_dir

        _make_exe(exes_dir, "10", "xiaoyu")
        _make_exe(exes_dir, "11", "xiaoyu")

        with pytest.raises(ValueError, match="多个账号"):
            find_ex_dir("xiaoyu")

    def test_with_owner_derives_account_from_layout(self, exes_dir):
        from config import find_ex_dir_with_owner

        _make_exe(exes_dir, "42", "mirror")
        path, owner = find_ex_dir_with_owner("mirror")
        assert owner == 42
        assert path.name == "mirror"

        _make_exe(exes_dir, "flatmirror")
        _path, owner = find_ex_dir_with_owner("flatmirror")
        assert owner is None


class TestCleanupCoversNestedLayout:
    def test_expired_conversation_in_nested_exe_is_cleaned(
        self, exes_dir, monkeypatch, capsys
    ):
        """回归：清理命令曾用 EXES_DIR.iterdir()，把账号 ID 当 slug，
        导致所有按账号隔离的镜像永远清理不到。"""
        monkeypatch.setattr("config.CONVERSATION_RETENTION_DAYS", 30)
        nested = _make_exe(exes_dir, "12", "xiaoyu")
        conv_file = _write_expired_conversation(nested)

        from commands.cleanup import cmd_cleanup

        cmd_cleanup("")

        assert "清理 1 条过期对话" in capsys.readouterr().out
        assert conv_file.read_text(encoding="utf-8") == ""
