"""evals 数据集测试：语料生成确定性、golden 标注有效性、加载校验。"""

import json

import pytest

from evals import build_corpus
from evals.dataset import load_corpus, load_golden


class TestBuildCorpus:
    """确定性生成与标注有效性。"""

    def test_deterministic(self):
        m1, g1 = build_corpus.build()
        m2, g2 = build_corpus.build()
        assert m1 == m2
        assert g1 == g2

    def test_corpus_shape(self):
        messages, golden = build_corpus.build()
        assert len(messages) >= 300
        assert 100 <= len(golden) <= 200
        # msg_id 连续、timestamp 唯一递增（chunk→消息映射依赖）
        timestamps = [m["timestamp"] for m in messages]
        assert [m["msg_id"] for m in messages] == list(range(len(messages)))
        assert timestamps == sorted(timestamps)
        assert len(set(timestamps)) == len(timestamps)

    def test_gold_ids_are_target_messages(self):
        messages, golden = build_corpus.build()
        for row in golden:
            assert row["gold_msg_ids"], f"{row['qid']} gold 为空"
            for mid in row["gold_msg_ids"]:
                assert messages[mid]["is_target"], (
                    f"{row['qid']} 的 gold 消息不是 target"
                )

    def test_qids_unique(self):
        _, golden = build_corpus.build()
        qids = [row["qid"] for row in golden]
        assert len(set(qids)) == len(qids)


class TestLoaders:
    """加载与校验逻辑（用临时文件构造非法数据）。"""

    def _write_jsonl(self, path, rows):
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _valid_msg(self, i, ts, is_target=True):
        return {
            "msg_id": i,
            "sender": "小雨" if is_target else "我",
            "content": f"消息{i}",
            "timestamp": ts,
            "is_target": is_target,
        }

    def test_load_corpus_ok(self, tmp_path):
        path = tmp_path / "corpus.jsonl"
        self._write_jsonl(
            path,
            [
                self._valid_msg(0, "2024-01-01 10:00:00"),
                self._valid_msg(1, "2024-01-01 10:01:00"),
            ],
        )
        assert len(load_corpus(path)) == 2

    def test_duplicate_timestamp_rejected(self, tmp_path):
        path = tmp_path / "corpus.jsonl"
        self._write_jsonl(
            path,
            [
                self._valid_msg(0, "2024-01-01 10:00:00"),
                self._valid_msg(1, "2024-01-01 10:00:00"),
            ],
        )
        with pytest.raises(ValueError, match="重复"):
            load_corpus(path)

    def test_non_sequential_msg_id_rejected(self, tmp_path):
        path = tmp_path / "corpus.jsonl"
        self._write_jsonl(path, [self._valid_msg(3, "2024-01-01 10:00:00")])
        with pytest.raises(ValueError, match="不连续"):
            load_corpus(path)

    def test_golden_non_target_gold_rejected(self, tmp_path):
        corpus_path = tmp_path / "corpus.jsonl"
        self._write_jsonl(
            corpus_path, [self._valid_msg(0, "2024-01-01 10:00:00", is_target=False)]
        )
        golden_path = tmp_path / "golden.jsonl"
        self._write_jsonl(
            golden_path,
            [
                {
                    "qid": "x-q1",
                    "query": "问题",
                    "category": "偏好",
                    "fact": "事实",
                    "gold_msg_ids": [0],
                }
            ],
        )
        corpus = load_corpus(corpus_path)
        with pytest.raises(ValueError, match="不是 target"):
            load_golden(golden_path, corpus=corpus)

    def test_golden_out_of_range_rejected(self, tmp_path):
        corpus_path = tmp_path / "corpus.jsonl"
        self._write_jsonl(corpus_path, [self._valid_msg(0, "2024-01-01 10:00:00")])
        golden_path = tmp_path / "golden.jsonl"
        self._write_jsonl(
            golden_path,
            [
                {
                    "qid": "x-q1",
                    "query": "问题",
                    "category": "偏好",
                    "fact": "事实",
                    "gold_msg_ids": [99],
                }
            ],
        )
        corpus = load_corpus(corpus_path)
        with pytest.raises(ValueError, match="越界"):
            load_golden(golden_path, corpus=corpus)
