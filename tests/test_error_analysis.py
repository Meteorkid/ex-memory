"""evals 错误分析（error_analysis）测试。

覆盖失败模式归因的纯函数与结果结构；全部用合成行，不依赖 build_corpus/MockEmbedder。
"""

from evals.error_analysis import (
    classify_failure,
    by_category,
    failure_mode_distribution,
    run_error_analysis,
)

# 注：字段口径与 ab_runner.per_query_prod 一致；holdout 行用 'relevant'，
# retrieval 行用 'relevant_chunks_target'。


def _row(
    qid: str,
    category: str,
    recall10: float,
    relevant: int = 1,
    mrr: float = 0.0,
) -> dict:
    return {
        "qid": qid,
        "category": category,
        "recall@10": recall10,
        "mrr": mrr,
        "relevant_chunks_target": relevant,
    }


class TestClassifyFailure:
    def test_perfect_returns_none(self):
        assert classify_failure(_row("q1", "偏好", 1.0), False) is None

    def test_heldout_priority(self):
        # 留出组优先：即便库中还有相关 fragment，也按预期失败归因
        assert classify_failure(_row("q1", "偏好", 0.0, relevant=2), True) == "heldout"

    def test_no_relevant_when_zero(self):
        assert classify_failure(_row("q1", "偏好", 0.0, relevant=0), False) == "no_relevant"

    def test_missed_evidence_when_zero_recall(self):
        assert classify_failure(_row("q1", "偏好", 0.0, relevant=3), False) == "missed_evidence"

    def test_partial_recall(self):
        assert classify_failure(_row("q1", "偏好", 0.5, relevant=2), False) == "partial_recall"


class TestByCategory:
    def test_groups_and_sorts_weakest_first(self):
        rows = [
            _row("q1", "偏好", 0.4),
            _row("q2", "偏好", 1.0),
            _row("q3", "工作学校", 1.0),
            _row("q4", "工作学校", 1.0),
        ]
        cats = by_category(rows)
        assert [c["category"] for c in cats] == ["偏好", "工作学校"]
        assert cats[0]["n"] == 2
        assert cats[0]["recall@10"] == 0.7
        assert cats[0]["n_failed"] == 1
        assert cats[0]["fail_rate"] == 0.5
        assert cats[1]["n_failed"] == 0

    def test_unknown_category_guard(self):
        rows = [_row("q1", None, 0.5)]
        cats = by_category(rows)
        assert cats[0]["category"] == "未知"


class TestDistribution:
    def test_counts_distinct_modes(self):
        rows = [
            _row("h1", "经历", 0.0, relevant=1),  # 留出
            _row("r1", "偏好", 0.0, relevant=0),  # 结构缺口
            _row("r2", "偏好", 0.0, relevant=2),  # 未召回
            _row("r3", "习惯", 0.5, relevant=2),  # 部分
            _row("ok", "习惯", 1.0, relevant=2),  # 无失败，忽略
        ]
        dist = failure_mode_distribution(rows, heldout_qids={"h1"})
        assert dist["heldout"] == 1
        assert dist["no_relevant"] == 1
        assert dist["missed_evidence"] == 1
        assert dist["partial_recall"] == 1
        assert "ok" in [r["qid"] for r in rows]


class TestRunErrorAnalysis:
    def test_structure_and_summary(self):
        retrieval = {
            "per_query_prod": [_row("q1", "偏好", 0.0, relevant=0)]
        }
        holdout = {
            "known": {"recall@10": 0.8},
            "heldout": {"recall@10": 0.0},
            "heldout_per_query": [],
        }
        out = run_error_analysis(retrieval, holdout)
        assert out["meta"]["n_queries"] == 1
        assert out["meta"]["n_failed"] == 1
        assert out["by_category"][0]["category"] == "偏好"
        assert out["failure_modes"] == {"no_relevant": 1}
        gen = out["generalization"]
        assert gen["passes"] is True
        assert gen["heldout_recall@10"] == 0.0
        assert any("最薄弱类别" in s for s in out["summary"])
        # 措辞不得把 recall=0 断言成「正确拒答」：那是生成端行为，检索指标无法证明。
        # 允许出现「不代表…正确拒答」这类否定澄清，禁止「被正确拒答」等肯定表述。
        assert any("留出切分有效" in s for s in out["summary"])
        forbidden = ["被正确拒答", "未见事实被正确拒答", "已正确拒答"]
        assert not any(f in s for s in out["summary"] for f in forbidden)

    def test_no_holdout_still_runs(self):
        retrieval = {"per_query_prod": [_row("q1", "偏好", 1.0)]}
        out = run_error_analysis(retrieval)
        assert out["generalization"] is None
        assert out["meta"]["n_failed"] == 0
        assert out["summary"] == ["本轮检索无失败查询"]