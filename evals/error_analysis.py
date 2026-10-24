"""错误分析：把检索/留出的逐条失败归因到类别与失败模式。

输入：
    retrieval:  evals/results/retrieval_results.json
                的 per_query_prod（生产配置逐条结果，字段见 ab_runner.run_retrieval_eval）
    holdout:    evals/results/holdout_results.json
                的 known_per_query / heldout_per_query 与 known/heldout 聚合

输出零散失败的四种归因（classify_failure）：
    heldout         - 该查询属于留出组（gold 已被剔除，预期失败，证明泛化测试生效）
    no_relevant     - 检索库中本就无相关 chunk（相关目标为 0，分块/过滤结构缺口）
    missed_evidence - 有相关 chunk 却没召回（recall@10 == 0，检索质量问题）
    partial_recall  - 只命中部分证据（0 < recall@10 < 1）

全部为纯函数，便于单测；不含副作用与 IO。
"""

from evals import metrics

# 留出组 / 已知组查询在 mapping 中的分组标记
GROUP_HELDOUT = "heldout"
GROUP_KNOWN = "known"

FAILURE_MODE_LABELS = {
    "heldout": "留出组（预期失败：知识已剔除）",
    "no_relevant": "库中无相关 chunk（结构缺口）",
    "missed_evidence": "有证据未召回（检索质量）",
    "partial_recall": "只命中部分证据",
}


def _relevant_count(row: dict) -> int:
    """兼容 retrieval（relevant_chunks_target）与 holdout（relevant）两种字段名。"""
    return int(row.get("relevant", row.get("relevant_chunks_target", 0)) or 0)


def is_failed(row: dict, threshold: float = 1.0) -> bool:
    """是否视为失败：recall@10 未完全命中。"""
    return row.get("recall@10", 1.0) < threshold


def classify_failure(row: dict, is_heldout: bool) -> str | None:
    """对失败查询判失败模式；非失败返回 None。"""
    if not is_failed(row):
        return None
    if is_heldout:
        return "heldout"
    if _relevant_count(row) == 0:
        return "no_relevant"
    if row.get("recall@10", 1.0) == 0.0:
        return "missed_evidence"
    return "partial_recall"


def by_category(retrieval_rows: list[dict]) -> list[dict]:
    """按类别聚合检索失败，最薄弱（recall@10 最低）排在最前。"""
    grouped: dict[str, list[dict]] = {}
    for row in retrieval_rows:
        cat = row.get("category") or "未知"
        grouped.setdefault(cat, []).append(row)
    out = []
    for cat, rows in grouped.items():
        rec = metrics.mean([r.get("recall@10", 1.0) for r in rows])
        failed = [r for r in rows if is_failed(r)]
        out.append(
            {
                "category": cat,
                "n": len(rows),
                "recall@10": rec,
                "mrr": metrics.mean([r.get("mrr", 0.0) for r in rows]),
                "n_failed": len(failed),
                "fail_rate": len(failed) / len(rows) if rows else 0.0,
            }
        )
    out.sort(key=lambda c: (c["recall@10"], c["category"]))
    return out


def failure_mode_distribution(
    retrieval_rows: list[dict], heldout_qids: set[str]
) -> dict[str, int]:
    """对全部失败查询做失败模式归因，返回 {mode: count}（按数量降序）。"""
    counts: dict[str, int] = {}
    for row in retrieval_rows:
        mode = classify_failure(row, row.get("qid") in heldout_qids)
        if mode is not None:
            counts[mode] = counts.get(mode, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def run_error_analysis(retrieval: dict, holdout: dict | None = None) -> dict:
    """汇总错误分析结果：类别薄弱点、失败模式分布、留出泛化判定与文字总结。

    Args:
        retrieval: run_retrieval_eval 结果（须含 per_query_prod）。
        holdout: run_holdout_eval 结果（可选；提供后用于泛化判定与留出标注）。
    """
    retrieval_rows = list(retrieval.get("per_query_prod", []))
    heldout_qids: set[str] = set()
    generalization: dict | None = None

    if holdout is not None:
        heldout_qids = {
            r["qid"] for r in holdout.get("heldout_per_query", [])
        }
        known_rec = holdout.get("known", {}).get("recall@10", 0.0)
        held_rec = holdout.get("heldout", {}).get("recall@10", 0.0)
        generalization = {
            "known_recall@10": known_rec,
            "heldout_recall@10": held_rec,
            "gap": known_rec - held_rec,
            "passes": held_rec <= 0.05 and known_rec > held_rec,
        }

    dist = failure_mode_distribution(retrieval_rows, heldout_qids)
    cats = by_category(retrieval_rows)
    failed_rows = [r for r in retrieval_rows if is_failed(r)]

    summary: list[str] = []
    if cats and failed_rows:
        weakest = cats[0]
        summary.append(
            f"最薄弱类别：{weakest['category']}（recall@10="
            f"{weakest['recall@10']:.1%}，{weakest['n_failed']}/{weakest['n']} 失败）"
        )
    if dist:
        top_mode, top_n = next(iter(dist.items()))
        summary.append(
            f"主导失败模式：{FAILURE_MODE_LABELS[top_mode]}（{top_n} 条）"
        )
    if generalization:
        # 注意：留出组 gold 消息已被移出检索库，recall=0 是构造性必然结果。
        # 它只能证明「剔除生效、切分无泄漏」，不涉及检索质量，更不能推断
        # 生成端是否「正确拒答」——那需要观察模型实际回答才能判断。
        verdict = (
            "留出切分有效：留出组召回≈0，被剔除的证据未被误召回"
            "（仅证明切分无泄漏；不涉及检索质量，也不代表模型能正确拒答）"
            if generalization["passes"]
            else "留出切分未完全通过：留出组仍有一定召回，需检查剔除是否生效"
        )
        summary.append(verdict)
    if not failed_rows:
        summary.append("本轮检索无失败查询")

    return {
        "meta": {
            "n_queries": len(retrieval_rows),
            "n_failed": len(failed_rows),
            "fail_rate": (
                len(failed_rows) / len(retrieval_rows) if retrieval_rows else 0.0
            ),
        },
        "by_category": cats,
        "failure_modes": dist,
        "failure_mode_labels": FAILURE_MODE_LABELS,
        "generalization": generalization,
        "summary": summary,
    }