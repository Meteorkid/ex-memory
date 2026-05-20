"""聊天记录切片器 — 测试。"""

from memory.chunker import Chunker


class TestChunkMessages:
    def test_empty_messages(self):
        chunker = Chunker()
        result = chunker.chunk_messages([], source="wechat", chat_id="test")
        assert result == []

    def test_single_chunk(self, sample_wechat_messages):
        chunker = Chunker()
        chunks = chunker.chunk_messages(
            sample_wechat_messages, source="wechat", chat_id="test", chunk_turns=10
        )
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["dominant_speaker"] == "target"
        assert chunks[0]["metadata"]["source"] == "wechat"

    def test_multiple_chunks(self, sample_wechat_messages):
        chunker = Chunker()
        chunks = chunker.chunk_messages(
            sample_wechat_messages, source="wechat", chat_id="test", chunk_turns=3, overlap_turns=0
        )
        assert len(chunks) >= 2

    def test_dominant_speaker_is_target(self, sample_target_heavy_messages):
        chunker = Chunker()
        chunks = chunker.chunk_messages(
            sample_target_heavy_messages, source="wechat", chat_id="test", chunk_turns=20
        )
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["dominant_speaker"] == "target"

    def test_chunk_has_required_fields(self, sample_wechat_messages):
        chunker = Chunker()
        chunks = chunker.chunk_messages(sample_wechat_messages, source="qq", chat_id="test")
        assert len(chunks) > 0
        for c in chunks:
            assert "id" in c
            assert "text_for_embedding" in c
            assert "display_text" in c
            assert "metadata" in c
            assert "source" in c["metadata"]
            assert "dominant_speaker" in c["metadata"]

    def test_overlap_between_chunks(self, sample_wechat_messages):
        chunker = Chunker()
        chunks = chunker.chunk_messages(
            sample_wechat_messages, source="wechat", chat_id="test",
            chunk_turns=3, overlap_turns=1,
        )
        if len(chunks) >= 2:
            # 有重叠时 chunk 数应比无重叠多
            non_overlap = chunker.chunk_messages(
                sample_wechat_messages, source="wechat", chat_id="test2",
                chunk_turns=3, overlap_turns=0,
            )
            assert len(chunks) >= len(non_overlap)


class TestChunkText:
    def test_empty_text(self):
        chunker = Chunker()
        result = chunker.chunk_text("", source="oral")
        assert result == []

    def test_single_chunk(self):
        chunker = Chunker()
        result = chunker.chunk_text("短文本", source="oral", chunk_chars=100)
        assert len(result) == 1
        assert result[0]["metadata"]["dominant_speaker"] == "narrative"

    def test_multiple_chunks(self):
        chunker = Chunker()
        long_text = "你好世界" * 300
        result = chunker.chunk_text(long_text, source="oral", chunk_chars=200, overlap_chars=0)
        assert len(result) > 1

    def test_chunks_not_exceed_chunk_chars(self):
        chunker = Chunker()
        text = "ABCDEFGHIJ" * 100
        result = chunker.chunk_text(text, source="oral", chunk_chars=800, overlap_chars=0)
        for c in result:
            assert len(c["text_for_embedding"]) <= 800
