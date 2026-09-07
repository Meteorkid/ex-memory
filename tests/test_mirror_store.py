"""core/mirror_store.py 门面测试（FR-034 前置）。

覆盖：本地后端全部读写方法与路径安全。config.EXES_DIR 由 conftest 的
isolate_exes_dir 指向临时目录，懒解析使门面天然跟随，不会碰真实 exes/。
"""

import pytest

from core.mirror_store import LocalMirrorBackend, mirror_store


@pytest.fixture
def store():
    return mirror_store("m1", owner=7)


def test_write_read_text_roundtrip(store):
    store.write_text("SKILL.md", "# 你好")
    assert store.read_text("SKILL.md") == "# 你好"


def test_write_read_bytes_roundtrip(store):
    store.write_bytes("blob.bin", b"\x00\x01\x02")
    assert store.read_bytes("blob.bin") == b"\x00\x01\x02"


def test_nested_rel_and_parents_created(store):
    store.write_text("sessions/2024/1/arch.md", "内容")
    assert store.exists("sessions/2024/1/arch.md")
    assert store.read_text("sessions/2024/1/arch.md") == "内容"


def test_write_read_json(store):
    store.write_json("meta.json", {"name": "m1", "owner_user_id": 7})
    assert store.read_json("meta.json")["owner_user_id"] == 7


def test_append_jsonl_and_list(store):
    store.append_jsonl("conversations/conversation.jsonl", [{"role": "user", "content": "a"}])
    store.append_jsonl("conversations/conversation.jsonl", [{"role": "assistant", "content": "b"}])
    assert store.exists("conversations/conversation.jsonl")
    files = store.list("conversations", "*.jsonl")
    assert files == ["conversations/conversation.jsonl"]
    assert not store.exists("missing.json")


def test_locked_update_json(store):
    store.write_json("archive_state.json", {"n": 0})
    store.locked_update_json("archive_state.json", {"n": 0}, lambda d: d.update({"n": d["n"] + 1}) or d)
    store.locked_update_json("archive_state.json", {"n": 0}, lambda d: d.update({"n": d["n"] + 1}) or d)
    assert store.read_json("archive_state.json")["n"] == 2


def test_locked_update_json_creates_missing(store):
    def _inc(d):
        d["n"] = d["n"] + 1
        return d

    store.locked_update_json("state.json", {"n": 0}, _inc)
    assert store.read_json("state.json") == {"n": 1}


def test_unlink(store):
    store.write_text("tmp.md", "x")
    store.unlink("tmp.md")
    assert not store.exists("tmp.md")


def test_mkdir_and_list_empty():
    s = mirror_store("empty", owner=9)
    s.mkdir("sessions")
    assert s.list("sessions") == []


def test_path_out_of_bounds_raises(store):
    # 相对路径可逃出镜像根（上级目录 / 平台分隔符归一化为 ".."）
    for bad in ("../evil", "a/../../evil", "x\\..\\..\\evil"):
        with pytest.raises(ValueError):
            store.path(bad)


def test_absolute_path_stays_under_root(store):
    # pathlib 的 joinpath 会把绝对路径前置到镜像根之下，因此不构成逃逸，
    # 门面只拦截真正的 ".." 穿越即可。
    p = store.path("meta.json")
    assert store.path("///meta.json") == p


def test_path_within_root_ok(store):
    p = store.path("meta.json")
    assert str(p).endswith("meta.json")
    assert p.is_relative_to(store.base_dir)


def test_base_dir_resolves_under_configured_exes_dir(store):
    import config

    assert store.base_dir.is_relative_to(config.EXES_DIR)
    # 嵌套：exes/<owner>/<slug>
    assert store.base_dir.name == "m1"


def test_backend_selected_from_config():
    s = mirror_store("x")
    assert s.name == "local"
    assert isinstance(s._backend, LocalMirrorBackend)  # noqa: SLF001 — 断言后端类型