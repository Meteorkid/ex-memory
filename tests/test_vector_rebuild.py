"""FR-008 回归：分块参数改为 3 轮/重叠 1，并提供向量重建命令。

依据 docs/eval_report.md 第 1 节：3 轮/重叠 1 的 Recall@5=91.5%，
5 轮/重叠 1 仅 71.1%；且 3 轮窗口下 target 候选池充足（turns8_overlap2
的 target 池会枯竭，Recall@5 崩到 21.1%）。
"""

from unittest.mock import patch

import config


class TestChunkParams:
    def test_production_chunk_params(self):
        assert config.CHUNK_TURNS == 3
        assert config.CHUNK_OVERLAP == 1

    def test_chunker_default_matches_3_1(self, sample_wechat_messages):
        """Chunker 默认参数即 (CHUNK_TURNS, CHUNK_OVERLAP)=(3,1)，
        生产摄入与检索评测 turns3_overlap1 配置行为完全一致。"""
        from memory.chunker import Chunker

        default = Chunker().chunk_messages(
            sample_wechat_messages, source="wechat", chat_id="t"
        )
        explicit = Chunker().chunk_messages(
            sample_wechat_messages,
            source="wechat",
            chat_id="t",
            chunk_turns=config.CHUNK_TURNS,
            overlap_turns=config.CHUNK_OVERLAP,
        )
        assert [c["id"] for c in default] == [c["id"] for c in explicit]


class TestCmdValidation:
    def test_missing_args_prints_usage(self, capsys):
        from commands import vector_rebuild

        vector_rebuild.cmd_vector_rebuild("")
        assert "用法" in capsys.readouterr().out

    def test_nonexistent_source(self, capsys, tmp_path):
        from commands import vector_rebuild

        ex_dir = tmp_path / "exes" / "a"
        ex_dir.mkdir(parents=True)
        with patch("config.EXES_DIR", tmp_path / "exes"):
            vector_rebuild.cmd_vector_rebuild(f"a {tmp_path}/nope.json")
        assert "源文件不存在" in capsys.readouterr().out

    def test_nonexistent_slug(self, capsys, tmp_path):
        from commands import vector_rebuild

        src = tmp_path / "x.json"
        src.write_text("[ ]", encoding="utf-8")
        with patch("config.EXES_DIR", tmp_path / "exes"):
            vector_rebuild.cmd_vector_rebuild(f"ghost {src}")
        assert "镜像 [ghost] 不存在" in capsys.readouterr().out


class TestRebuildBehavior:
    def test_rebuild_clears_before_reingest(self, tmp_path):
        """重建先清空旧 collection，再按当前参数重新入库，避免新旧参数切片混存。"""
        from commands import vector_rebuild

        ex_dir = tmp_path / "exes" / "a"
        (ex_dir / "chroma_db").mkdir(parents=True)
        (ex_dir / "SKILL.md").write_text("x", encoding="utf-8")
        (ex_dir / "meta.json").write_text("{}", encoding="utf-8")
        src = tmp_path / "a.json"
        src.write_text(
            '[{"sender": "target", "content": "hi", "timestamp": "1"}]',
            encoding="utf-8",
        )

        calls = []

        class FakeStore:
            def __init__(self, persist_dir, collection_name):
                calls.append(("init", persist_dir, collection_name))

            def delete_collection(self):
                calls.append(("delete",))

        emb_cfg = {"api_key": "k", "base_url": "u", "model": "m"}
        with (
            patch("config.EXES_DIR", tmp_path / "exes"),
            patch("config.get_embedding_config", return_value=emb_cfg),
            patch("memory.vector_store.VectorStore", FakeStore),
            patch(
                "memory.ingest.ingest_wechat_file",
                return_value=([{"sender": "target"}], 7),
            ) as ing,
        ):
            msg = vector_rebuild._rebuild_from_file("a", src)

        # 首次创建旧 collection → 清空 → 重建后入库
        assert calls[0][0] == "init"
        assert ("delete",) in calls
        assert ing.call_count == 1
        assert calls[-1][0] == "init"
        assert "7 个切片" in msg


class TestCorpusArchive:
    """语料归档：让向量重建不依赖用户再找一遍原始文件。

    改造前导入是「解析 → 切片 → 向量化 → 丢弃原始消息」，源文件也在请求
    结束时删掉。后果是换 embedding 模型、调分块参数、迁移向量库都会卡死，
    因为没有可重放的输入。
    """

    def test_ingest_archives_masked_messages(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock, patch

        from core.corpus_store import load_messages

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "arc").mkdir(parents=True)

        with (
            patch(
                "parsers.wechat_parser.parse",
                return_value=[
                    {"sender": "ta", "content": "电话 13812345678", "is_target": True}
                ],
            ),
            patch("memory.chunker.Chunker.chunk_messages", return_value=[]),
        ):
            from memory.ingest import ingest_wechat_file

            ingest_wechat_file("x.json", "arc", "ta", MagicMock(), MagicMock())

        archived = load_messages("arc")
        assert len(archived) == 1
        # 归档的是脱敏后的内容，不引入新的暴露面
        assert "13812345678" not in archived[0]["content"]
        assert archived[0]["_source"] == "wechat"

    def test_repeated_imports_append_not_overwrite(self, tmp_path, monkeypatch):
        from core.corpus_store import append_messages, count_messages

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "multi").mkdir(parents=True)

        append_messages("multi", [{"content": "第一份"}], source="wechat")
        append_messages("multi", [{"content": "第二份"}], source="qq")
        assert count_messages("multi") == 2

    def test_corrupt_lines_are_skipped_not_fatal(self, tmp_path, monkeypatch):
        from core.corpus_store import corpus_path, load_messages

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "bad").mkdir(parents=True)
        corpus_path("bad").write_text(
            '{"content": "好行"}\n这不是 JSON\n{"content": "另一好行"}\n',
            encoding="utf-8",
        )
        assert len(load_messages("bad")) == 2

    def test_rebuild_from_corpus_needs_no_source_file(self, tmp_path, monkeypatch):
        """核心价值：重建不再需要用户翻出当初那个文件。"""
        from unittest.mock import MagicMock, patch

        from commands import vector_rebuild
        from core.corpus_store import append_messages

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "rb").mkdir(parents=True)
        append_messages(
            "rb",
            [
                {"sender": "ta", "content": f"第{i}句", "is_target": True}
                for i in range(6)
            ],
            source="wechat",
        )

        store = MagicMock()
        with (
            patch.object(
                vector_rebuild, "_fresh_store", return_value=(store, MagicMock())
            ),
            patch(
                "memory.chunker.Chunker.chunk_messages",
                return_value=[{"text": "c1"}, {"text": "c2"}],
            ),
        ):
            msg = vector_rebuild._rebuild_from_corpus("rb")

        assert "重放 6 条归档消息" in msg
        assert "2 个切片" in msg
        store.ingest.assert_called_once()

    def test_rebuild_from_corpus_rejects_empty_archive(self, tmp_path, monkeypatch):
        import pytest as _pytest

        from commands import vector_rebuild

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "empty").mkdir(parents=True)
        with _pytest.raises(RuntimeError, match="语料归档为空"):
            vector_rebuild._rebuild_from_corpus("empty")

    def test_has_corpus_reports_legacy_mirrors(self, tmp_path, monkeypatch):
        """改造前导入的存量镜像没有归档，只能走源文件路径。"""
        from core.corpus_store import has_corpus

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "legacy").mkdir(parents=True)
        assert has_corpus("legacy") is False
