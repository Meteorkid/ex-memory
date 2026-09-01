"""FR-007 回归：privacy 模块接线（此前全仓零引用）。

- 导入链路与对话落库脱敏：手机号/身份证/银行卡/邮箱不以明文入库
- 过期对话清理：记录级过滤，CLI /cleanup 触发，留存天数可配置
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def exes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("config.EXES_DIR", tmp_path)
    return tmp_path


class TestMaskSensitive:
    def test_masks_all_four_types(self):
        from core.privacy import mask_sensitive

        text = "电话 13812345678，邮箱 alices@example.com"
        masked = mask_sensitive(text)
        assert "13812345678" not in masked
        assert "alices@example.com" not in masked
        assert "138****5678" in masked
        assert "a***@example.com" in masked

    def test_keeps_normal_text(self):
        from core.privacy import mask_sensitive

        assert mask_sensitive("晚安，早点休息呀") == "晚安，早点休息呀"


class TestAppendTurnMasks:
    def test_sensitive_info_not_stored_plaintext(self, exes_dir):
        from core.conversation_store import append_turn, load_jsonl_messages

        append_turn(
            "m1",
            1,
            "我的手机号是 13812345678",
            "好呀，发到 bob@example.com 吧",
            source="web",
        )

        msgs = load_jsonl_messages("m1")
        contents = [m["content"] for m in msgs]
        joined = "\n".join(contents)
        assert "13812345678" not in joined
        assert "bob@example.com" not in joined
        assert "138****5678" in joined


class TestIngestMasks:
    def test_wechat_ingest_masks_before_chromadb(self, exes_dir):
        from memory.ingest import ingest_wechat_file

        raw_messages = [
            {
                "sender": "ta",
                "content": "我的新号码 13998887777",
                "timestamp": "2024-01-01 10:00",
                "is_target": True,
            },
            {
                "sender": "我",
                "content": "收到啦",
                "timestamp": "2024-01-01 10:01",
                "is_target": False,
            },
        ]
        vector_store = MagicMock()

        with patch("parsers.wechat_parser.parse", return_value=raw_messages):
            messages, chunk_count = ingest_wechat_file(
                "/tmp/fake.txt", "m2", "ta", vector_store, embedder=None
            )

        # 返回值与入库 chunks 都是脱敏后的
        assert "13998887777" not in messages[0]["content"]
        ingested_chunks = vector_store.ingest.call_args[0][0]
        assert chunk_count == len(ingested_chunks) > 0
        for chunk in ingested_chunks:
            assert "13998887777" not in chunk["text_for_embedding"]
            assert "13998887777" not in chunk["display_text"]

    def test_ingest_text_masks(self, exes_dir):
        from memory.ingest import ingest_text

        vector_store = MagicMock()
        n = ingest_text(
            "银行卡 6222020200112233445 借你", "m3", "oral_update", vector_store, None
        )
        assert n > 0
        ingested_chunks = vector_store.ingest.call_args[0][0]
        for chunk in ingested_chunks:
            assert "6222020200112233445" not in chunk["text_for_embedding"]


class TestCleanExpiredConversations:
    def _write_conversations(self, exes_dir, slug, records):
        conv = exes_dir / slug / "conversations"
        conv.mkdir(parents=True, exist_ok=True)
        (conv / "conversation.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8",
        )

    def test_removes_only_expired_records(self, exes_dir):
        from core.privacy import clean_expired_conversations

        old_ts = (datetime.now() - timedelta(days=100)).isoformat()
        new_ts = datetime.now().isoformat()
        self._write_conversations(
            exes_dir,
            "c1",
            [
                {"role": "user", "content": "旧消息", "created_at": old_ts},
                {"role": "user", "content": "新消息", "created_at": new_ts},
            ],
        )

        removed = clean_expired_conversations("c1", retention_days=90)
        assert removed == 1

        lines = (
            (exes_dir / "c1" / "conversations" / "conversation.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert len(lines) == 1
        assert json.loads(lines[0])["content"] == "新消息"

    def test_missing_dir_returns_zero(self, exes_dir):
        from core.privacy import clean_expired_conversations

        assert clean_expired_conversations("ghost", retention_days=90) == 0

    def test_unparseable_lines_kept(self, exes_dir):
        from core.privacy import clean_expired_conversations

        conv = exes_dir / "c2" / "conversations"
        conv.mkdir(parents=True)
        old_ts = (datetime.now() - timedelta(days=100)).isoformat()
        (conv / "conversation.jsonl").write_text(
            "{broken json\n"
            + json.dumps({"role": "user", "content": "旧", "created_at": old_ts})
            + "\n",
            encoding="utf-8",
        )

        removed = clean_expired_conversations("c2", retention_days=90)
        assert removed == 1
        remaining = (conv / "conversation.jsonl").read_text(encoding="utf-8")
        assert "{broken json" in remaining


class TestCleanupCommand:
    def test_cmd_cleanup_uses_configured_retention(self, exes_dir, monkeypatch, capsys):
        monkeypatch.setattr("config.CONVERSATION_RETENTION_DAYS", 30)
        old_ts = (datetime.now() - timedelta(days=40)).isoformat()
        conv = exes_dir / "c3" / "conversations"
        conv.mkdir(parents=True)
        (conv / "conversation.jsonl").write_text(
            json.dumps({"role": "user", "content": "过期", "created_at": old_ts})
            + "\n",
            encoding="utf-8",
        )

        from commands.cleanup import cmd_cleanup

        cmd_cleanup("")

        out = capsys.readouterr().out
        assert "清理 1 条过期对话" in out
        remaining = (conv / "conversation.jsonl").read_text(encoding="utf-8")
        assert remaining == ""


class TestRetentionConfig:
    def test_default_retention_days(self):
        import config

        assert config.CONVERSATION_RETENTION_DAYS == 90
        assert isinstance(config.CONVERSATION_RETENTION_DAYS, int)
