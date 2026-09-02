"""对象存储与平台级备份恢复（FR-034 部分 / NFR-033）。

镜像自带的 /backup 是单个镜像的版本快照，存在同一块盘上：磁盘坏了、容器
重建了，它跟着一起没。这里测的是灾备意义上的备份。
"""

import json

import pytest

from core import blob_store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    blob_store.reset_for_tests()
    blob_store.configure(local_root=tmp_path / "blobs")
    yield tmp_path
    blob_store.reset_for_tests()


def _make_mirror(root, owner, slug, files):
    d = root / "exes" / str(owner) / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps({"name": slug, "owner_user_id": owner}), encoding="utf-8"
    )
    for name, content in files.items():
        target = d / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return d


class TestLocalBackend:
    def test_roundtrip(self, env):
        store = blob_store.backend()
        store.put("a/b.txt", b"hello")
        assert store.get("a/b.txt") == b"hello"
        assert store.exists("a/b.txt") is True

    def test_missing_key_returns_none(self, env):
        assert blob_store.backend().get("nope") is None

    def test_delete(self, env):
        store = blob_store.backend()
        store.put("x", b"1")
        store.delete("x")
        assert store.exists("x") is False

    def test_path_traversal_is_rejected(self, env):
        """key 由内部生成，但恢复流程会读到外部数据。"""
        with pytest.raises(ValueError):
            blob_store.backend().put("../escaped", b"x")

    def test_list_keys_filters_by_prefix(self, env):
        store = blob_store.backend()
        store.put("p1/a", b"1")
        store.put("p1/b", b"2")
        store.put("p2/c", b"3")
        assert sorted(store.list_keys("p1/")) == ["p1/a", "p1/b"]

    def test_put_is_atomic_no_tmp_leftovers(self, env):
        store = blob_store.backend()
        store.put("k", b"v")
        assert not list(env.joinpath("blobs").rglob("*.tmp"))


class TestSnapshotRestore:
    def test_snapshot_captures_all_files(self, env):
        _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "人格", "corpus.jsonl": "语料"})
        assert blob_store.snapshot_mirror("xiaoyu", 7, "s1") == 3

    def test_restore_rebuilds_deleted_mirror(self, env):
        """容器重建或磁盘丢失后能恢复——这是备份存在的理由。"""
        import shutil

        d = _make_mirror(
            env, 7, "xiaoyu", {"SKILL.md": "ta 的人格", "sessions/a.md": "摘要"}
        )
        blob_store.snapshot_mirror("xiaoyu", 7, "s1")

        shutil.rmtree(d)
        assert not d.exists()

        assert blob_store.restore_mirror("xiaoyu", 7, "s1") == 3
        assert (d / "SKILL.md").read_text(encoding="utf-8") == "ta 的人格"
        assert (d / "sessions" / "a.md").read_text(encoding="utf-8") == "摘要"

    def test_restore_replaces_corrupted_current_state(self, env):
        d = _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "正确内容"})
        blob_store.snapshot_mirror("xiaoyu", 7, "s1")
        (d / "SKILL.md").write_text("被写坏了", encoding="utf-8")

        blob_store.restore_mirror("xiaoyu", 7, "s1")
        assert (d / "SKILL.md").read_text(encoding="utf-8") == "正确内容"

    def test_restore_is_atomic_on_missing_snapshot(self, env):
        """快照不存在时不能把现有镜像弄没。"""
        d = _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "现有内容"})
        with pytest.raises(FileNotFoundError):
            blob_store.restore_mirror("xiaoyu", 7, "不存在的标签")
        assert (d / "SKILL.md").read_text(encoding="utf-8") == "现有内容"

    def test_snapshots_are_isolated_by_label(self, env):
        d = _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "第一版"})
        blob_store.snapshot_mirror("xiaoyu", 7, "v1")
        (d / "SKILL.md").write_text("第二版", encoding="utf-8")
        blob_store.snapshot_mirror("xiaoyu", 7, "v2")

        blob_store.restore_mirror("xiaoyu", 7, "v1")
        assert (d / "SKILL.md").read_text(encoding="utf-8") == "第一版"

    def test_lock_and_tmp_files_are_skipped(self, env):
        d = _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "人格"})
        (d / "conversation.jsonl.lock").write_text("", encoding="utf-8")
        (d / "x.tmp").write_text("", encoding="utf-8")
        assert blob_store.snapshot_mirror("xiaoyu", 7, "s1") == 2

    def test_list_snapshots(self, env):
        _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "x"})
        blob_store.snapshot_mirror("xiaoyu", 7, "a")
        blob_store.snapshot_mirror("xiaoyu", 7, "b")
        assert blob_store.list_snapshots() == ["a", "b"]


class TestVerify:
    def test_verify_passes_after_snapshot(self, env, capsys):
        _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "人格"})
        from commands.backup_platform import cmd_snapshot

        cmd_snapshot("--label t1 --verify")
        out = capsys.readouterr().out
        assert "校验通过" in out

    def test_verify_detects_missing_backup(self, env, capsys):
        """没演练过的备份等于没有备份——校验要真取回来比对。"""
        _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "人格"})
        from commands.backup_platform import _verify

        result = _verify("从未备份过的标签")
        assert "校验失败" in result and "缺失" in result

    def test_verify_detects_content_drift(self, env):
        d = _make_mirror(env, 7, "xiaoyu", {"SKILL.md": "人格"})
        blob_store.snapshot_mirror("xiaoyu", 7, "t2")
        (d / "SKILL.md").write_text("本地改了但没重新备份", encoding="utf-8")

        from commands.backup_platform import _verify

        assert "不一致" in _verify("t2")


class TestDegradation:
    def test_bad_s3_config_falls_back_to_local(self, env, monkeypatch):
        """配错了要降级并告警，不能让服务起不来。"""
        blob_store.reset_for_tests()

        def boom(*a, **k):
            raise RuntimeError("凭证无效")

        monkeypatch.setattr(blob_store, "S3Backend", boom)
        backend = blob_store.configure(bucket="whatever", local_root=env / "blobs2")
        assert backend.name == "local"
