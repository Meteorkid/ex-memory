"""人物记忆闭环的持久会话、跨实例版本和可恢复归档。"""

import subprocess
import sys
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    from core.mirror_store import mirror_store

    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    store = mirror_store("demo", 7)
    store.write_json("meta.json", {"owner_user_id": 7})
    store.write_text("SKILL.md", "# 人物\n\n---\n\n## PART B\n规则")
    return store


def test_conversations_remain_separate_and_restore_after_new_reader(mirror):
    from core.conversation_store import append_turn, load_history, list_conversations

    append_turn("demo", 7, "网页问题", "网页回答", conversation_id="web")
    append_turn(
        "demo",
        7,
        "群内问题",
        "群内回答",
        source="astrbot",
        conversation_id="im:group:one",
        conversation_label="测试群",
    )
    assert load_history("demo", 7, "web") == [
        {"role": "user", "content": "网页问题"},
        {"role": "assistant", "content": "网页回答"},
    ]
    assert load_history("demo", 7, "im:group:one")[-1]["content"] == "群内回答"
    items = {c["conversation_id"]: c for c in list_conversations("demo", 7)}
    assert items["im:group:one"]["label"] == "测试群"
    assert items["web"]["message_count"] == 2
    assert load_history("demo", 8, "im:group:one") == []


def test_legacy_records_belong_only_to_web_and_have_stable_read_ids(mirror):
    from core.conversation_store import load_jsonl_messages, get_message

    raw = {"role": "assistant", "content": "旧回复"}
    mirror.append_jsonl("conversations/conversation.jsonl", [raw])
    assert load_jsonl_messages("demo", 7) == [raw]
    first = load_jsonl_messages("demo", 7, conversation_id="web")[0]
    second = load_jsonl_messages("demo", 7, conversation_id="web")[0]
    assert first["id"] == second["id"]
    assert get_message("demo", 7, first["id"], "web")["content"] == "旧回复"
    assert load_jsonl_messages("demo", 7, conversation_id="im:one") == []


@pytest.mark.parametrize("value", ["", "../escape", "a/b", "a\\b", "a" * 129, 3])
def test_bad_conversation_ids_rejected_before_write(mirror, value):
    from core.conversation_store import append_turn

    with pytest.raises(ValueError):
        append_turn("demo", 7, "q", "a", conversation_id=value)
    assert not mirror.exists("conversations/conversation.jsonl")


def test_saved_sources_are_bound_to_answer_and_masked(mirror):
    from core.conversation_store import append_turn, get_message

    sources = [{"id": "source-1", "text": "电话13812345678", "kind": "original"}]
    turn_id = append_turn("demo", 7, "q", "a", conversation_id="web", sources=sources)
    answer = get_message("demo", 7, turn_id + "-assistant", "web")
    assert answer["sources"][0]["text"] == "电话138****5678"
    assert get_message("demo", 7, turn_id + "-assistant", "im:other") is None
    assert sources[0]["text"] == "电话13812345678"


def test_history_is_bounded_without_changing_saved_messages(mirror):
    from core.conversation_store import append_turn, load_history, load_jsonl_messages

    for n in range(5):
        append_turn("demo", 7, f"q{n}", f"a{n}", conversation_id="web")
    assert load_history("demo", 7, "web", limit=4) == [
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "a4"},
    ]
    assert len(load_jsonl_messages("demo", 7)) == 10


def test_correction_is_atomic_and_records_reference_context(mirror):
    from core.corrections import add, load

    context = {"conversation_id": "web", "message_id": "reply-1", "source_ids": ["s1"]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(
            pool.map(lambda _: add("demo", "ta不用昵称", 7, context=context), range(18))
        )
    entries = load("demo", 7)
    assert len(entries) == 1
    assert entries[0]["count"] == 18
    assert entries[0]["context"] == context


def test_revision_is_visible_to_new_instances_and_changes_on_same_value_write(mirror):
    from core.memory_state import get_memory_revision
    from core.mirror_store import mirror_store

    before = get_memory_revision("demo", 7)
    mirror_store("demo", 7).write_text("persona.md", "简短")
    first = get_memory_revision("demo", 7)
    mirror_store("demo", 7).write_text("persona.md", "简短")
    assert first > before
    assert get_memory_revision("demo", 7) > first
    assert get_memory_revision("demo", 8) == 0


def test_revision_tracks_rollback_and_correction_writes(mirror):
    from core.memory_state import get_memory_revision
    from core.corrections import add
    from core.version_manager import backup, rollback

    mirror.write_text("persona.md", "旧画像")
    version = backup("demo", "snapshot", include_chroma=False, owner=7)
    mirror.write_text("persona.md", "新画像")
    before = get_memory_revision("demo", 7)
    rollback("demo", version, owner=7)
    assert mirror.read_text("persona.md") == "旧画像"
    assert get_memory_revision("demo", 7) > before
    before = get_memory_revision("demo", 7)
    add("demo", "ta不用昵称", 7)
    assert get_memory_revision("demo", 7) > before


def test_revision_is_visible_in_another_process(mirror):
    from core.corrections import add
    from core.memory_state import get_memory_revision
    import config

    add("demo", "ta不用昵称", 7)
    script = (
        "import config,sys; from pathlib import Path; "
        "config.EXES_DIR=Path(sys.argv[1]); "
        "from core.memory_state import get_memory_revision; "
        "print(get_memory_revision('demo',7))"
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(config.EXES_DIR)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert int(completed.stdout.strip()) == get_memory_revision("demo", 7)


def test_conversation_request_lock_is_nonblocking_and_scoped(mirror):
    from core.conversation_store import conversation_request_lock

    with conversation_request_lock("demo", 7, "web"):
        with pytest.raises(TimeoutError):
            with conversation_request_lock("demo", 7, "web"):
                pytest.fail("第二个实例不应拿到同会话锁")
        with conversation_request_lock("demo", 7, "im:other"):
            pass
    with conversation_request_lock("demo", 7, "web"):
        pass


@pytest.fixture
def summary_model(monkeypatch):
    state = {"calls": 0, "failure": False}

    def create(**kwargs):
        state["calls"] += 1
        if state["failure"]:
            raise RuntimeError("synthetic summary failure")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="可恢复的会话摘要"))
            ]
        )

    monkeypatch.setattr(
        "config.get_llm_config",
        lambda *_args: {"api_key": "synthetic", "model": "synthetic"},
    )
    monkeypatch.setattr(
        "config.get_llm_client",
        lambda *_args: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    return state


def test_scoped_archive_never_changes_global_memory_or_other_conversation(
    mirror, summary_model
):
    from core.conversation_store import append_turn
    from core.session_archive import (
        maybe_archive,
        load_conversation_summaries,
        get_archive_status,
    )

    global_skill = mirror.read_text("SKILL.md")
    append_turn("demo", 7, "群问题", "群回答", conversation_id="im:group")
    assert (
        maybe_archive("demo", owner=7, threshold=1, conversation_id="im:group") is True
    )
    summaries = load_conversation_summaries("demo", 7, "im:group")
    assert summaries[0]["text"] == "可恢复的会话摘要"
    assert summaries[0]["kind"] == "generated_summary"
    assert summaries[0]["source"] == "session_summary"
    assert load_conversation_summaries("demo", 7, "web") == []
    assert mirror.read_text("SKILL.md") == global_skill
    assert get_archive_status("demo", 7, "im:group")["status"] == "complete"


def test_summary_failure_does_not_advance_cursor_and_retry_uses_same_batch(
    mirror, summary_model
):
    from core.conversation_store import append_turn
    from core.session_archive import (
        maybe_archive,
        get_archive_status,
        load_conversation_summaries,
    )

    append_turn("demo", 7, "q", "a", conversation_id="web")
    summary_model["failure"] = True
    maybe_archive("demo", owner=7, threshold=1, conversation_id="web")
    failed = get_archive_status("demo", 7, "web")
    assert failed["status"] == "failed"
    assert failed["archived_messages"] == 0
    assert failed["pending_turns"] == 1
    summary_model["failure"] = False
    assert maybe_archive("demo", owner=7, threshold=1, conversation_id="web") is True
    completed = get_archive_status("demo", 7, "web")
    assert completed["batch_id"] == failed["batch_id"]
    assert completed["archived_messages"] == 2
    assert len(load_conversation_summaries("demo", 7, "web")) == 1
    assert maybe_archive("demo", owner=7, threshold=1, conversation_id="web") is False
    assert summary_model["calls"] == 2


def test_concurrent_scoped_archive_summarizes_once(mirror, summary_model):
    from core.conversation_store import append_turn
    from core.session_archive import maybe_archive, load_conversation_summaries

    append_turn("demo", 7, "q", "a", conversation_id="web")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(
                lambda _: maybe_archive(
                    "demo", owner=7, threshold=1, conversation_id="web"
                ),
                range(5),
            )
        )
    assert results.count(True) == 1
    assert summary_model["calls"] == 1
    assert len(load_conversation_summaries("demo", 7, "web")) == 1


def test_archive_commit_retry_reuses_saved_summary(mirror, summary_model, monkeypatch):
    from core.conversation_store import append_turn
    from core.mirror_store import MirrorStore
    from core.session_archive import (
        maybe_archive,
        get_archive_status,
        load_conversation_summaries,
    )

    append_turn("demo", 7, "q", "a", conversation_id="web")
    original = MirrorStore.write_json
    failures = []

    def fail_completion_once(store, rel, data):
        if (
            rel.endswith("/state.json")
            and data.get("status") == "complete"
            and not failures
        ):
            failures.append(True)
            raise OSError("synthetic commit failure")
        return original(store, rel, data)

    monkeypatch.setattr(MirrorStore, "write_json", fail_completion_once)
    assert maybe_archive("demo", owner=7, threshold=1, conversation_id="web") is False
    assert get_archive_status("demo", 7, "web")["archived_messages"] == 0
    assert maybe_archive("demo", owner=7, threshold=1, conversation_id="web") is True
    assert summary_model["calls"] == 1
    assert len(load_conversation_summaries("demo", 7, "web")) == 1


def test_legacy_archive_never_promotes_scoped_chat_to_shared_memory(
    mirror, monkeypatch
):
    from core.conversation_store import append_turn
    from core.session_archive import maybe_archive

    append_turn("demo", 7, "群聊私密资料", "群聊回复", conversation_id="im:group")
    append_turn("demo", 7, "网页问题", "网页回复")
    captured = []

    def create(**kwargs):
        captured.append(kwargs["messages"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="只总结网页"))]
        )

    monkeypatch.setattr(
        "config.get_llm_config",
        lambda *a: {"api_key": "synthetic", "model": "synthetic"},
    )
    monkeypatch.setattr(
        "config.get_llm_client",
        lambda *a: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        ),
    )
    assert maybe_archive("demo", owner=7, threshold=1) is True
    assert "网页问题" in str(captured)
    assert "群聊私密资料" not in str(captured)
    assert "群聊私密资料" not in mirror.read_text("SKILL.md")


def test_recreated_mirror_signature_cannot_reuse_old_generation(mirror):
    from core.memory_state import get_memory_signature, get_memory_revision

    before = get_memory_signature("demo", 7)
    revision = get_memory_revision("demo", 7)
    shutil.rmtree(mirror.base_dir)
    mirror.write_json("meta.json", {"owner_user_id": 7})
    mirror.write_text("SKILL.md", "新的镜像")
    assert get_memory_revision("demo", 7) == revision
    assert get_memory_signature("demo", 7) != before


def test_old_revision_signature_is_read_only_and_changes_after_recreation(mirror):
    from core.memory_state import STATE_FILE, get_memory_signature

    mirror.write_json(STATE_FILE, {"revision": 5})
    original = mirror.read_text(STATE_FILE)
    signature = get_memory_signature("demo", 7)
    assert get_memory_signature("demo", 7) == signature
    assert mirror.read_text(STATE_FILE) == original
    shutil.rmtree(mirror.base_dir)
    mirror.mkdir("")
    mirror.write_json(STATE_FILE, {"revision": 5})
    assert get_memory_signature("demo", 7) != signature
    assert mirror.read_text(STATE_FILE) == original


def test_legacy_ids_and_archive_cursor_survive_retention_cleanup(mirror, monkeypatch):
    from datetime import datetime, timedelta
    from core.conversation_store import load_jsonl_messages, get_message
    from core.privacy import clean_expired_conversations
    from core.session_archive import maybe_archive, get_archive_status

    old = (datetime.now() - timedelta(days=100)).isoformat()
    recent = datetime.now().isoformat()
    mirror.append_jsonl(
        "conversations/conversation.jsonl",
        [
            {"role": "user", "content": "过期问", "created_at": old},
            {"role": "assistant", "content": "过期答", "created_at": old},
            {"role": "user", "content": "保留问", "created_at": recent},
            {"role": "assistant", "content": "保留答", "created_at": recent},
        ],
    )
    monkeypatch.setattr(
        "core.session_archive._generate_conversation_summary", lambda *a: "已归档"
    )
    assert maybe_archive("demo", owner=7, conversation_id="web", threshold=1)
    previous_id = load_jsonl_messages("demo", 7, "web")[-1]["id"]
    assert clean_expired_conversations("demo", owner=7) == 2
    assert load_jsonl_messages("demo", 7, "web")[-1]["id"] == previous_id
    assert get_message("demo", 7, previous_id, "web")["content"] == "保留答"
    assert get_archive_status("demo", 7, "web")["pending_turns"] == 0


def test_legacy_ids_ignore_json_format_but_distinguish_equal_rows(mirror):
    from core.conversation_store import load_jsonl_messages

    record = {"role": "assistant", "content": "相同内容", "created_at": "2026-10-29"}
    path = "conversations/conversation.jsonl"
    mirror.append_jsonl(path, [record, record])
    before = [m["id"] for m in load_jsonl_messages("demo", 7, "web")]
    mirror.write_text(
        path,
        "\n".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) for _ in range(2)
        )
        + "\n",
    )
    after = [m["id"] for m in load_jsonl_messages("demo", 7, "web")]
    assert before[0] != before[1]
    assert after == before
