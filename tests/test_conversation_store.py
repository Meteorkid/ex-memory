"""conversation_store 测试：append_turn / load_jsonl_messages（原为零覆盖路径）。"""

import json
import pytest


@pytest.fixture
def exes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("config.EXES_DIR", tmp_path)
    return tmp_path


def _read_raw(exes_dir, slug: str, owner=None) -> list[str]:
    base = exes_dir / str(owner) / slug if owner is not None else exes_dir / slug
    path = base / "conversations" / "conversation.jsonl"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


class TestAppendTurn:
    def test_appends_user_and_assistant_records(self, exes_dir):
        from core.conversation_store import append_turn

        append_turn(
            "alpha",
            user_id=7,
            user_message="在吗",
            assistant_reply="在的",
            stickers=["builtin_happy_laugh"],
            source="web",
        )

        lines = _read_raw(exes_dir, "alpha", owner=7)
        assert len(lines) == 2

        user_rec = json.loads(lines[0])
        assistant_rec = json.loads(lines[1])

        assert user_rec["role"] == "user"
        assert user_rec["content"] == "在吗"
        assert user_rec["user_id"] == 7
        assert user_rec["source"] == "web"
        assert user_rec["turn_id"] == assistant_rec["turn_id"]
        assert user_rec["id"].endswith("-user")
        assert user_rec["created_at"]

        assert assistant_rec["role"] == "assistant"
        assert assistant_rec["content"] == "在的"
        assert assistant_rec["stickers"] == ["builtin_happy_laugh"]
        assert assistant_rec["id"].endswith("-assistant")

    def test_default_stickers_empty_and_source_web(self, exes_dir):
        from core.conversation_store import append_turn

        append_turn("alpha", 7, "q", "a")
        assistant_rec = json.loads(_read_raw(exes_dir, "alpha", owner=7)[1])
        assert assistant_rec["stickers"] == []
        assert assistant_rec["source"] == "web"

    def test_turns_accumulate(self, exes_dir):
        from core.conversation_store import append_turn

        append_turn("alpha", 7, "q1", "a1")
        append_turn("alpha", 7, "q2", "a2")
        assert len(_read_raw(exes_dir, "alpha", owner=7)) == 4


class TestLoadJsonlMessages:
    def test_roundtrip_and_missing_dir(self, exes_dir):
        from core.conversation_store import append_turn, load_jsonl_messages

        assert load_jsonl_messages("ghost") == []

        append_turn("alpha", 7, "q", "a")
        msgs = load_jsonl_messages("alpha", owner=7)
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert [m["content"] for m in msgs] == ["q", "a"]

    def test_skips_corrupt_lines_and_unknown_roles(self, exes_dir):
        from core.conversation_store import load_jsonl_messages

        conv = exes_dir / "beta" / "conversations"
        conv.mkdir(parents=True)
        (conv / "conversation.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"role": "user", "content": "ok"}),
                    "{broken json",
                    json.dumps({"role": "system", "content": "skipped"}),
                    json.dumps({"role": "assistant", "content": ""}),  # 空 content 跳过
                ]
            ),
            encoding="utf-8",
        )
        msgs = load_jsonl_messages("beta")
        assert msgs == [{"role": "user", "content": "ok"}]
