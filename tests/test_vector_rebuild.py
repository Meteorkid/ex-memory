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
            msg = vector_rebuild._rebuild("a", src)

        # 首次创建旧 collection → 清空 → 重建后入库
        assert calls[0][0] == "init"
        assert ("delete",) in calls
        assert ing.call_count == 1
        assert calls[-1][0] == "init"
        assert "7 个切片" in msg
