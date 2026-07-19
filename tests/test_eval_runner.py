"""evals 运行链路测试：chunk→消息映射、缓存、离线端到端冒烟（不依赖网络）。"""

import pytest

from evals import build_corpus
from evals.ab_runner import (
    CachingEmbedder,
    ChunkConfig,
    MockEmbedder,
    chunk_msg_range,
    run_retrieval_eval,
)
from evals.dataset import GoldenItem
from evals.generation_eval import sample_golden
from evals.judge import _parse_claims


def _golden_items(rows):
    return [
        GoldenItem(
            qid=r["qid"],
            query=r["query"],
            category=r["category"],
            fact=r["fact"],
            gold_msg_ids=frozenset(r["gold_msg_ids"]),
        )
        for r in rows
    ]


class TestChunkMsgRange:
    """chunk 的 start_ts/end_ts 还原为连续 msg_id 区间。"""

    def test_mapping_matches_chunk_windows(self):
        from memory.chunker import Chunker

        messages = [
            {
                "msg_id": i,
                "sender": "小雨",
                "content": f"内容{i}",
                "timestamp": f"2024-01-01 10:{i:02d}:00",
                "is_target": True,
            }
            for i in range(6)
        ]
        ts_to_id = {m["timestamp"]: m["msg_id"] for m in messages}
        chunks = Chunker().chunk_messages(
            messages, source="t", chat_id="t", chunk_turns=2, overlap_turns=0
        )
        assert len(chunks) == 3
        assert chunk_msg_range(chunks[0]["metadata"], ts_to_id) == {0, 1}
        assert chunk_msg_range(chunks[1]["metadata"], ts_to_id) == {2, 3}
        assert chunk_msg_range(chunks[2]["metadata"], ts_to_id) == {4, 5}


class TestEmbedders:
    def test_mock_deterministic_and_normalized(self):
        emb = MockEmbedder()
        v1 = emb.embed_one("你好世界")
        v2 = emb.embed_one("你好世界")
        assert v1 == v2
        assert sum(x * x for x in v1) == pytest.approx(1.0)

    def test_caching_embedder_dedupes_calls(self):
        calls = []

        class CountingBase:
            def embed(self, texts):
                calls.append(list(texts))
                return [[1.0, 0.0]] * len(texts)

        emb = CachingEmbedder(CountingBase())
        emb.embed(["a", "b", "a"])
        emb.embed_one("a")
        emb.embed_one("b")
        # 只有第一次 embed 真正调用底层，且去重后只传 a/b
        assert calls == [["a", "b"]]


class TestSampleGolden:
    def test_no_duplicate_cluster_and_deterministic(self):
        _, rows = build_corpus.build()
        golden = _golden_items(rows)
        s1 = sample_golden(golden, 20)
        s2 = sample_golden(golden, 20)
        assert s1 == s2
        assert len(s1) == 20
        fids = [g.qid.rsplit("-q", 1)[0] for g in s1]
        assert len(set(fids)) == 20

    def test_limit_over_size_returns_all(self):
        _, rows = build_corpus.build()
        golden = _golden_items(rows)
        assert len(sample_golden(golden, len(golden) + 10)) == len(golden)


class TestJudgeParsing:
    def test_valid(self):
        raw = (
            '{"claims": [{"text": "喜欢奶茶", "supported_by_context": true, '
            '"gold": "support"}]}'
        )
        claims = _parse_claims(raw)
        assert claims == [
            {"text": "喜欢奶茶", "supported_by_context": True, "gold": "support"}
        ]

    def test_empty_claims_ok(self):
        assert _parse_claims('{"claims": []}') == []

    def test_invalid_gold_value_rejected(self):
        with pytest.raises(ValueError, match="gold"):
            _parse_claims('{"claims": [{"text": "x", "gold": "maybe"}]}')

    def test_missing_claims_rejected(self):
        with pytest.raises(ValueError, match="claims"):
            _parse_claims('{"foo": 1}')


class TestRetrievalEvalOffline:
    """MockEmbedder 端到端冒烟：验证结构与数值边界，不验证检索质量。"""

    @pytest.fixture(scope="class")
    def results(self, tmp_path_factory):
        messages, rows = build_corpus.build()
        golden = _golden_items(rows)[:8]
        return run_retrieval_eval(
            messages,
            golden,
            MockEmbedder(),
            chunk_configs=[ChunkConfig(3, 1), ChunkConfig(5, 1)],
            persist_dir=str(tmp_path_factory.mktemp("chroma")),
        )

    def test_structure(self, results):
        assert results["meta"]["n_queries"] == 8
        assert [c["label"] for c in results["configs"]] == [
            "turns3_overlap1",
            "turns5_overlap1",
        ]
        for cfg in results["configs"]:
            assert cfg["n_chunks"] > 0
            assert 0 < cfg["n_target_chunks"] <= cfg["n_chunks"]
            assert len(cfg["threshold_sweep"]) == 6

    def test_metric_bounds(self, results):
        for cfg in results["configs"]:
            for path in ("unfiltered", "target_only"):
                for key, value in cfg[path].items():
                    assert 0.0 <= value <= 1.0, f"{cfg['label']}.{path}.{key}={value}"

    def test_per_query_prod_populated(self, results):
        rows = results["per_query_prod"]
        assert len(rows) == 8
        for row in rows:
            assert row["first_hit_rank"] is None or 1 <= row["first_hit_rank"] <= 10

    def test_recall_monotonic_in_k(self, results):
        for cfg in results["configs"]:
            t = cfg["target_only"]
            assert t["recall@1"] <= t["recall@3"] <= t["recall@5"] <= t["recall@10"]
