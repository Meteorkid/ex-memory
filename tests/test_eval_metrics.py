"""evals.metrics 手写指标测试：已知输入验证计算结果与边界情况。"""

import math

import pytest

from evals import metrics


class TestRecallAtK:
    """消息级 Recall@K。"""

    def test_full_recall(self):
        ranked = [{1, 2}, {3}]
        assert metrics.recall_at_k(ranked, {1, 2, 3}, k=2) == 1.0

    def test_partial_recall(self):
        ranked = [{1}, {9}, {2}]
        # top-2 只覆盖 gold 中的 1
        assert metrics.recall_at_k(ranked, {1, 2}, k=2) == 0.5

    def test_k_truncation(self):
        ranked = [{9}, {1, 2}]
        assert metrics.recall_at_k(ranked, {1, 2}, k=1) == 0.0

    def test_empty_retrieval(self):
        assert metrics.recall_at_k([], {1}, k=5) == 0.0

    def test_empty_gold_raises(self):
        with pytest.raises(ValueError):
            metrics.recall_at_k([{1}], set(), k=5)

    def test_invalid_k_raises(self):
        with pytest.raises(ValueError):
            metrics.recall_at_k([{1}], {1}, k=0)


class TestPrecisionAtK:
    """Precision@K，分母固定为 k。"""

    def test_half_hit(self):
        ranked = [{1}, {9}]
        assert metrics.precision_at_k(ranked, {1}, k=2) == 0.5

    def test_short_list_denominator_is_k(self):
        # 只返回 1 条且命中，precision@5 = 1/5
        assert metrics.precision_at_k([{1}], {1}, k=5) == pytest.approx(0.2)


class TestMRR:
    """Mean Reciprocal Rank 单查询项。"""

    def test_first_hit(self):
        assert metrics.mrr([{1}, {9}], {1}) == 1.0

    def test_third_hit(self):
        assert metrics.mrr([{9}, {8}, {1}], {1}) == pytest.approx(1 / 3)

    def test_no_hit(self):
        assert metrics.mrr([{9}], {1}) == 0.0


class TestNDCG:
    """二值相关性 nDCG@K。"""

    def test_ideal_ranking(self):
        ranked = [{1}, {2}, {9}]
        assert metrics.ndcg_at_k(
            ranked, {1, 2}, k=3, total_relevant=2
        ) == pytest.approx(1.0)

    def test_worse_ranking_lower(self):
        ideal = metrics.ndcg_at_k([{1}, {9}], {1}, k=2, total_relevant=1)
        worse = metrics.ndcg_at_k([{9}, {1}], {1}, k=2, total_relevant=1)
        assert ideal == pytest.approx(1.0)
        # 命中在第 2 位: dcg = 1/log2(3), idcg = 1
        assert worse == pytest.approx(1 / math.log2(3))
        assert worse < ideal

    def test_no_relevant_chunks(self):
        assert metrics.ndcg_at_k([{9}], {1}, k=5, total_relevant=0) == 0.0


class TestHitAtK:
    def test_hit(self):
        assert metrics.hit_at_k([{9}, {1}], {1}, k=2) == 1.0

    def test_miss_outside_k(self):
        assert metrics.hit_at_k([{9}, {1}], {1}, k=1) == 0.0


class TestFaithfulness:
    """生成指标：忠实度与幻觉率聚合。"""

    def test_score_fraction(self):
        claims = [{"supported": True}, {"supported": False}]
        assert metrics.faithfulness_score(claims) == 0.5

    def test_no_claims_returns_none(self):
        assert metrics.faithfulness_score([]) is None

    def test_aggregate_generation(self):
        per_answer = [
            [{"supported": True}, {"supported": True}],  # 忠实
            [{"supported": True}, {"supported": False}],  # 有幻觉
            [],  # 无论断，剔除
        ]
        agg = metrics.aggregate_generation(per_answer)
        assert agg["faithfulness"] == pytest.approx(0.75)
        assert agg["hallucination_rate"] == pytest.approx(0.5)
        assert agg["n_scored"] == 2
        assert agg["n_no_claim"] == 1

    def test_aggregate_gold_consistency(self):
        per_answer = [
            [{"gold": "support"}, {"gold": "support"}],
            [{"gold": "support"}, {"gold": "absent"}],
            [{"gold": "contradict"}],
            [],
        ]
        agg = metrics.aggregate_gold_consistency(per_answer)
        assert agg["hallucination_rate"] == pytest.approx(2 / 3)
        assert agg["claim_accuracy"] == pytest.approx(3 / 5)
        assert agg["n_scored"] == 3
        assert agg["n_no_claim"] == 1

    def test_contradiction_and_unsupported_are_separated(self):
        """把「记错了」和「多说了一句」合成一个数，前者就被后者淹没了。

        实测 RAG 链路严格口径 17.5%，其中真正的事实错误只有 3.5%——差别大到
        会影响「达标没达标」的判断，所以两者必须分列。
        """
        per_answer = [
            [{"gold": "support"}],
            [{"gold": "support"}, {"gold": "absent"}],
            [{"gold": "contradict"}],
            [{"gold": "contradict"}, {"gold": "absent"}],
        ]
        agg = metrics.aggregate_gold_consistency(per_answer)
        assert agg["hallucination_rate"] == pytest.approx(3 / 4)
        assert agg["contradiction_rate"] == pytest.approx(2 / 4)
        # 同时含 contradict 与 absent 的回答只计入矛盾，不重复计数
        assert agg["unsupported_rate"] == pytest.approx(1 / 4)

    def test_rates_do_not_double_count(self):
        """三个比率的口径：矛盾与仅未支持互斥，两者之和等于严格口径。"""
        per_answer = [
            [{"gold": "contradict"}, {"gold": "absent"}],
            [{"gold": "absent"}],
            [{"gold": "support"}],
        ]
        agg = metrics.aggregate_gold_consistency(per_answer)
        assert agg["contradiction_rate"] + agg["unsupported_rate"] == pytest.approx(
            agg["hallucination_rate"]
        )

    def test_aggregate_empty(self):
        agg = metrics.aggregate_generation([])
        assert agg["faithfulness"] == 0.0
        assert agg["n_scored"] == 0


class TestMean:
    def test_mean(self):
        assert metrics.mean([1.0, 2.0, 3.0]) == 2.0

    def test_empty(self):
        assert metrics.mean([]) == 0.0
