"""evals 事实簇留出（holdout）评测测试。

核心不变量：从检索库剔除某事实簇的消息后，该簇查询（heldout）应被判为
『库外无相关』（低召回），而未受影响的事实簇查询（known）应保持正常召回。
"""

import pytest

from evals import build_corpus
from evals.ab_runner import MockEmbedder
from evals.dataset import GoldenItem
from evals.holdout import (
    holdout_facts,
    run_holdout_eval,
    split_holdout,
)


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


# 固定用一个真实存在的事实簇做留出，保证测试确定性、不依赖抽样内部
HELD_FACT = "最喜欢的奶茶是乌龙玛奇朵，三分糖去冰"


class TestSplitHoldout:
    def test_removes_only_heldout_target_messages(self):
        messages, rows = build_corpus.build()
        golden = _golden_items(rows)
        known_corpus, known_q, heldout_q, removed = split_holdout(
            messages, golden, {HELD_FACT}
        )

        # 留出簇的消息确实存在且有抽取
        assert removed, "留出事实簇应命中若干 target 消息"
        # 已知语料不再包含任何被剔消息，且被剔消息本身地位于语料内
        kept_ids = {m["msg_id"] for m in known_corpus}
        assert kept_ids.isdisjoint(removed)
        assert removed <= {m["msg_id"] for m in messages}

        # known / heldout 查询恰好覆盖全部 golden，且互斥
        heldout_qids = {g.qid for g in heldout_q}
        known_qids = {g.qid for g in known_q}
        assert heldout_qids.isdisjoint(known_qids)
        assert heldout_qids | known_qids == {g.qid for g in golden}
        assert heldout_qids, "留出簇应至少命中一组查询"
        assert known_qids, "非留出簇查询不应为空"

    def test_split_deterministic(self):
        messages, rows = build_corpus.build()
        golden = _golden_items(rows)
        a = split_holdout(messages, golden, {HELD_FACT})
        b = split_holdout(messages, golden, {HELD_FACT})
        assert a == b

    def test_holdout_facts_sampling(self):
        _, rows = build_corpus.build()
        golden = _golden_items(rows)
        facts = holdout_facts(golden, n=3, seed=7)
        assert len(facts) == 3
        assert facts <= {g.fact for g in golden}


class TestRunHoldoutEvalOffline:
    @pytest.fixture(scope="class")
    def results(self, tmp_path_factory):
        messages, rows = build_corpus.build()
        golden = _golden_items(rows)
        return run_holdout_eval(
            messages,
            golden,
            MockEmbedder(),
            heldout_facts={HELD_FACT},
            persist_dir=str(tmp_path_factory.mktemp("holdout_chroma")),
        )

    def test_structure(self, results):
        assert set(results) == {"known", "heldout", "meta"}
        assert results["meta"]["n_known"] > 0
        assert results["meta"]["n_heldout"] > 0
        assert results["meta"]["config"] == "turns5_overlap1"

    def test_metric_bounds(self, results):
        for group in ("known", "heldout"):
            for key, value in results[group].items():
                assert 0.0 <= value <= 1.0, f"{group}.{key}={value}"

    def test_heldout_recall_near_zero(self, results):
        # 留出知识已从库中移除：检索不应再命中该簇的黄金消息
        assert results["heldout"]["recall@10"] <= 0.05

    def test_known_beats_heldout(self, results):
        # 同一检索库下，已知簇召回应显著高于留出簇
        assert results["known"]["recall@10"] >= results["heldout"]["recall@10"]
        assert results["known"]["mrr"] >= results["heldout"]["mrr"]