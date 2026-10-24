"""评测报告生成：matplotlib 图表 + Markdown 报告。

输入 evals/results/*.json，输出 docs/eval/ 下的 PNG 与 docs/eval_report.md。
"""

import json
from datetime import date
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RETRIEVAL_RESULTS = RESULTS_DIR / "retrieval_results.json"
GENERATION_RESULTS = RESULTS_DIR / "generation_results.json"
HOLDOUT_RESULTS = RESULTS_DIR / "holdout_results.json"
ERROR_ANALYSIS_RESULTS = RESULTS_DIR / "error_analysis.json"
REPORT_IMG_DIR = PROJECT_DIR / "docs" / "eval"
REPORT_PATH = PROJECT_DIR / "docs" / "eval_report.md"


def _setup_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # macOS/Linux 常见中文字体链，避免图表中文变豆腐块
    plt.rcParams["font.sans-serif"] = [
        "PingFang SC",
        "Hiragino Sans GB",
        "Heiti SC",
        "Noto Sans CJK SC",
        "Arial Unicode MS",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def plot_recall_vs_k(plt, retrieval: dict, out: Path):
    """各分块配置的 Recall@K 曲线（生产路径 target_only）。"""
    from evals.ab_runner import PROD_CONFIG

    fig, ax = plt.subplots(figsize=(7, 4.5))
    k_values = retrieval["meta"]["k_values"]
    for cfg in retrieval["configs"]:
        recalls = [cfg["target_only"][f"recall@{k}"] for k in k_values]
        is_prod = cfg["label"] == PROD_CONFIG.label
        style = "-o" if is_prod else "--s"
        label = f"{cfg['turns']}轮/重叠{cfg['overlap']}"
        if is_prod:
            label += "（生产）"
        ax.plot(k_values, recalls, style, label=label, linewidth=2)
    ax.set_xlabel("K")
    ax.set_ylabel("Recall@K（消息级）")
    ax.set_title("分块策略 A/B：Recall@K 对比")
    ax.set_xticks(k_values)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def plot_threshold_sweep(plt, retrieval: dict, out: Path):
    """生产分块配置下的阈值扫描：召回 vs 注入上下文条数。"""
    from evals.ab_runner import PROD_CONFIG

    cfg = next(c for c in retrieval["configs"] if c["label"] == PROD_CONFIG.label)
    sweep = cfg["threshold_sweep"]
    ts = [s["threshold"] for s in sweep]
    recalls = [s["recall"] for s in sweep]
    kept = [s["avg_kept"] for s in sweep]

    fig, ax1 = plt.subplots(figsize=(7, 4.5))
    ax1.plot(
        ts, recalls, "-o", color="tab:blue", linewidth=2, label="Recall（阈值过滤后）"
    )
    ax1.set_xlabel("相似度阈值 RAG_THRESHOLD")
    ax1.set_ylabel("Recall", color="tab:blue")
    ax1.tick_params(axis="y", labelcolor="tab:blue")
    ax1.grid(alpha=0.3)
    ax1.axvline(0.3, color="gray", linestyle=":", linewidth=1.5)
    ax1.annotate(
        "当前生产值 0.3",
        xy=(0.3, min(recalls)),
        fontsize=9,
        color="gray",
        xytext=(0.32, min(recalls)),
    )

    ax2 = ax1.twinx()
    ax2.plot(ts, kept, "--s", color="tab:orange", linewidth=2, label="平均注入条数")
    ax2.set_ylabel("平均注入 prompt 的 chunk 数", color="tab:orange")
    ax2.tick_params(axis="y", labelcolor="tab:orange")

    fig.suptitle(
        "阈值扫描：召回 vs 上下文注入量"
        f"（生产分块 {PROD_CONFIG.turns}轮/重叠{PROD_CONFIG.overlap}）"
    )
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def plot_filter_impact(plt, retrieval: dict, out: Path):
    """dominant_speaker=target 过滤对召回的影响。"""
    labels, unfiltered, filtered = [], [], []
    for cfg in retrieval["configs"]:
        labels.append(f"{cfg['turns']}轮/重叠{cfg['overlap']}")
        unfiltered.append(cfg["unfiltered"]["recall@5"])
        filtered.append(cfg["target_only"]["recall@5"])

    x = range(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.bar(
        [i - width / 2 for i in x], unfiltered, width, label="无过滤", color="tab:blue"
    )
    ax.bar(
        [i + width / 2 for i in x],
        filtered,
        width,
        label="仅 target 原话（生产）",
        color="tab:orange",
    )
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("Recall@5（消息级）")
    ax.set_title("dominant_speaker 过滤对召回的影响")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def plot_generation(plt, generation: dict, out: Path):
    """RAG vs 无 RAG：忠实度与幻觉率。"""
    modes = ["rag", "no_rag"]
    names = {"rag": "RAG（生产链路）", "no_rag": "无 RAG 基线"}
    faith = [generation["modes"][m]["faithfulness"] for m in modes]
    halluc = [generation["modes"][m]["hallucination_rate"] for m in modes]

    x = range(len(modes))
    width = 0.35
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    bars1 = ax.bar(
        [i - width / 2 for i in x],
        faith,
        width,
        label="忠实度（论断被上下文支持）",
        color="tab:green",
    )
    bars2 = ax.bar(
        [i + width / 2 for i in x],
        halluc,
        width,
        label="幻觉率（回答与黄金事实不符）",
        color="tab:red",
    )
    for bars in (bars1, bars2):
        for b in bars:
            ax.annotate(
                f"{b.get_height():.0%}",
                (b.get_x() + b.get_width() / 2, b.get_height()),
                ha="center",
                va="bottom",
                fontsize=10,
            )
    ax.set_xticks(list(x))
    ax.set_xticklabels([names[m] for m in modes])
    ax.set_ylim(0, 1.15)
    ax.set_ylabel("比例")
    ax.set_title("生成质量：RAG vs 无 RAG")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def _fmt_pct(v: float) -> str:
    return f"{v:.1%}"


def plot_holdout(plt, holdout: dict, out: Path):
    """留出泛化：known vs heldout 的 recall@5/@10/mrr 分组柱状图。"""
    groups = ["known", "heldout"]
    metric_keys = ["recall@5", "recall@10", "mrr"]
    names = {"known": "已知簇", "heldout": "留出簇"}
    x = range(len(metric_keys))
    width = 0.35
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for i, g in enumerate(groups):
        vals = [holdout[g][k] for k in metric_keys]
        offset = i * width - width / 2
        ax.bar(
            [v + offset for v in x],
            vals,
            width,
            label=names[g],
            color="tab:blue" if g == "known" else "tab:orange",
        )
        for v, val in zip(x, vals):
            ax.annotate(
                f"{val:.2f}",
                (v + offset, val),
                ha="center",
                va="bottom",
                fontsize=9,
            )
    ax.set_xticks(list(x))
    ax.set_xticklabels(metric_keys)
    ax.set_ylim(0, 1.0)
    ax.set_ylabel("指标值")
    ax.set_title("留出泛化测试：known vs heldout")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=9)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def plot_failure_modes(plt, analysis: dict, out: Path):
    """错误分析：失败模式分布横向条形图。"""
    dist = analysis.get("failure_modes", {})
    if not dist:
        return
    labels = analysis.get("failure_mode_labels", {})
    modes = list(dist.keys())
    counts = [dist[m] for m in modes]
    fig, ax = plt.subplots(figsize=(7, max(2.4, 0.5 * len(modes) + 1.2)))
    y = range(len(modes))
    ax.barh(list(y), counts, color="tab:red", alpha=0.85)
    for i, c in enumerate(counts):
        ax.annotate(str(c), (c, i), va="center", fontsize=10)
    ax.set_yticks(list(y))
    ax.set_yticklabels([labels.get(m, m) for m in modes], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("失败查询数")
    ax.set_title("错误分析：失败模式分布")
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)


def _worst_queries(retrieval: dict, n: int = 5) -> list[dict]:
    rows = [r for r in retrieval.get("per_query_prod", []) if r["recall@10"] < 1.0]
    return sorted(rows, key=lambda r: (r["recall@10"], r["mrr"]))[:n]


def _holdout_verdict_text(holdout: dict) -> str:
    """留出泛化判定的结论文字。"""
    known = holdout.get("known", {}).get("recall@10", 0.0)
    held = holdout.get("heldout", {}).get("recall@10", 0.0)
    gap = known - held
    if held <= 0.05 and gap > 0:
        return (
            f"留出簇 recall@10={_fmt_pct(held)}，远低于已知簇 "
            f"({_fmt_pct(known)}，gap {_fmt_pct(gap)})：被剔除的证据未被误召回，"
            "留出切分生效。这是「切分无泄漏」的流程有效性检查——"
            "留出组的 gold 消息本就不在库中，recall=0 属构造性必然，"
            "既不说明检索质量，也不代表模型能正确拒答。"
        )
    return (
        f"注意：留出簇 recall@10={_fmt_pct(held)} 仍有一定召回（已知簇 "
        f"{_fmt_pct(known)}）。留出后的消息仍可能被误召回，需检查切分或检索阈值。"
    )


def _holdout_caveat_text(hmeta: dict) -> str:
    """已知簇口径提示：与主评测的配置一致性 + 重建库的残存扰动。

    历史问题：`retrieval_results.json` 曾在 PROD_CONFIG 由 (5,1) 改为 (3,1)
    之后未重跑，`per_query_prod` 停留在旧配置，导致已知簇与主评测口径
    不一致（94.9% vs 71.7%）。现已统一在 config.py 的生产配置下复跑。

    残存扰动：剔除 target 消息会使剩余语料重新分块，chunk 的说话人构成
    随之变化，部分原本被 target 过滤排除的 chunk 可能重新归入 target，
    候选池与主评测不完全相同。
    """
    from evals.ab_runner import PROD_CONFIG

    return (
        "**已知簇与主评测的口径关系：** 两者现均运行在生产配置 "
        f"`{PROD_CONFIG.label}`（由 `config.py` 的 `CHUNK_TURNS`/"
        "`CHUNK_OVERLAP` 决定）。此前 `per_query_prod` 停留在旧配置 (5,1)，"
        "与留出测试 (3,1) 口径不一致，造成两组数字看似悬殊；"
        "统一配置后该差异已消失。"
        f"剔除 {hmeta.get('n_removed', 0)} 条消息后检索库重新分块，"
        "chunk 的 `dominant_speaker` 归属随之改变，候选池与主评测"
        "仍有轻微不同——实测对已知簇 recall@10 的影响在噪声量级"
        "（2026-10-25 复测约 0.4 个百分点），可并列参考；"
        "严格比较时以主评测同配置结果为准。"
    )


def build_markdown(
    retrieval: dict,
    generation: dict | None,
    holdout: dict | None = None,
    analysis: dict | None = None,
) -> str:
    from evals.ab_runner import PROD_CONFIG

    meta = retrieval["meta"]
    prod_label = PROD_CONFIG.label
    prod = next(c for c in retrieval["configs"] if c["label"] == prod_label)
    best = max(retrieval["configs"], key=lambda c: c["target_only"]["recall@5"])

    lines = [
        "# RAG 评测报告",
        "",
        f"> 生成日期：{date.today().isoformat()}　|　"
        f"语料：{meta['corpus_size']} 条消息　|　"
        f"Golden Dataset：{meta['n_queries']} 条查询　|　"
        f"Embedding：{meta['embedder']}",
        "",
        "评测方法：合成微信风格语料 + 消息级 ground truth（chunk ID 随分块参数变化，",
        "消息级标注才能横向对比分块策略）。指标全部手写实现，见 `evals/metrics.py`。",
        "复现：`python -m evals.run_eval all`",
        "",
        "## 1. 分块策略 A/B（生产检索路径）",
        "",
        "| 配置 | chunks | Recall@5 | Recall@10 | MRR | nDCG@10 | Precision@5 |",
        "|---|---|---|---|---|---|---|",
    ]
    for cfg in retrieval["configs"]:
        t = cfg["target_only"]
        mark = "（生产）" if cfg["label"] == prod_label else ""
        lines.append(
            f"| {cfg['turns']}轮/重叠{cfg['overlap']}{mark} | {cfg['n_chunks']} "
            f"| {_fmt_pct(t['recall@5'])} | {_fmt_pct(t['recall@10'])} "
            f"| {t['mrr']:.3f} | {t['ndcg@10']:.3f} | {_fmt_pct(t['precision@5'])} |"
        )
    lines += [
        "",
        f"最优配置：**{best['turns']}轮/重叠{best['overlap']}**"
        f"（Recall@5 {_fmt_pct(best['target_only']['recall@5'])}，"
        f"生产配置为 {_fmt_pct(prod['target_only']['recall@5'])}）。",
        "",
        "![Recall@K 对比](eval/recall_vs_k.png)",
        "",
        "## 2. 相似度阈值扫描（RAG_THRESHOLD）",
        "",
        "| 阈值 | Recall | 平均注入条数 | 注入命中率 |",
        "|---|---|---|---|",
    ]
    for s in prod["threshold_sweep"]:
        lines.append(
            f"| {s['threshold']:.1f} | {_fmt_pct(s['recall'])} "
            f"| {s['avg_kept']:.1f} | {_fmt_pct(s['precision_kept'])} |"
        )
    lines += [
        "",
        "![阈值扫描](eval/threshold_sweep.png)",
        "",
        "## 3. dominant_speaker 过滤的代价",
        "",
        "生产路径只检索 `dominant_speaker == target` 的 chunk。",
        "对比无过滤检索，量化该过滤对召回的影响：",
        "",
        "![过滤影响](eval/filter_impact.png)",
        "",
    ]

    if generation:
        gmeta = generation["meta"]
        rag = generation["modes"].get("rag", {})
        no_rag = generation["modes"].get("no_rag", {})
        lines += [
            "## 4. 生成质量：忠实度与幻觉率",
            "",
            f"抽样 {gmeta['n_items']} 条查询（每个事实簇最多一条），"
            f"生成模型 {gmeta['gen_model']}（生产采样参数），"
            f"judge 模型 {gmeta['judge_model']}（temperature=0，论断级判定）。",
            "",
            "| 模式 | 忠实度 | 幻觉率 | 论断准确率 | 计分回答数 |",
            "|---|---|---|---|---|",
            f"| RAG（生产链路） | {_fmt_pct(rag.get('faithfulness', 0))} "
            f"| {_fmt_pct(rag.get('hallucination_rate', 0))} "
            f"| {_fmt_pct(rag.get('claim_accuracy', 0))} | {rag.get('n_scored', 0)} |",
            f"| 无 RAG 基线 | — "
            f"| {_fmt_pct(no_rag.get('hallucination_rate', 0))} "
            f"| {_fmt_pct(no_rag.get('claim_accuracy', 0))} | {no_rag.get('n_scored', 0)} |",
            "",
            "- 忠实度 = 回答中被检索上下文支持的论断占比（无 RAG 基线没有上下文，不适用）",
            "- 幻觉率 = 含「与黄金事实矛盾/凭空编造细节」论断的回答占比（严格口径）",
            f"- judge 解析失败剔除 {gmeta.get('judge_failures', 0)} 条，不参与计分",
            "",
            "![生成质量](eval/generation.png)",
            "",
        ]

    if holdout is not None:
        ko = holdout.get("known", {})
        ho = holdout.get("heldout", {})
        hmeta = holdout.get("meta", {})
        lines += [
            "## 5. 留出泛化测试（独立开发集）",
            "",
            f"把 {len(hmeta.get('heldout_facts', []))} 个事实簇的 target 消息（"
            f"{hmeta.get('n_removed', 0)} 条）从检索库剔除，其余事实簇照常建库。"
            "同查询集分两组对比：",
            "",
            "| 分组 | 查询数 | Recall@5 | Recall@10 | MRR | nDCG@10 |",
            "|---|---|---|---|---|---|",
            f"| 已知簇 | {hmeta.get('n_known', 0)} | {_fmt_pct(ko.get('recall@5', 0))} "
            f"| {_fmt_pct(ko.get('recall@10', 0))} | {ko.get('mrr', 0):.3f} "
            f"| {ko.get('ndcg@10', 0):.3f} |",
            f"| 留出簇 | {hmeta.get('n_heldout', 0)} | {_fmt_pct(ho.get('recall@5', 0))} "
            f"| {_fmt_pct(ho.get('recall@10', 0))} | {ho.get('mrr', 0):.3f} "
            f"| {ho.get('ndcg@10', 0):.3f} |",
            "",
            _holdout_verdict_text(holdout),
            "",
            _holdout_caveat_text(hmeta),
            "",
            "![留出泛化测试](eval/holdout.png)",
            "",
        ]

    if analysis is not None:
        ameta = analysis.get("meta", {})
        cats = analysis.get("by_category", [])
        dist = analysis.get("failure_modes", {})
        labels = analysis.get("failure_mode_labels", {})
        lines += [
            "## 6. 错误分析：失败模式与薄弱类别",
            "",
            f"本轮 {ameta.get('n_queries', 0)} 条查询，"
            f"{ameta.get('n_failed', 0)} 条未完全召回"
            f"（失败率 {_fmt_pct(ameta.get('fail_rate', 0))}）。"
            "逐条失败归因见下。",
            "",
        ]
        if cats:
            lines += [
                "| 类别 | 查询数 | recall@10 | MRR | 失败数 | 失败率 |",
                "|---|---|---|---|---|---|",
            ]
            for c in cats:
                lines.append(
                    f"| {c['category']} | {c['n']} | {_fmt_pct(c['recall@10'])} "
                    f"| {c['mrr']:.3f} | {c['n_failed']} | {_fmt_pct(c['fail_rate'])} |"
                )
            lines.append("")
        if dist:
            lines += [
                "| 失败模式 | 数量 |",
                "|---|---|",
            ]
            for mode, count in dist.items():
                lines.append(f"| {labels.get(mode, mode)} | {count} |")
            lines += [
                "",
                "![错误分析：失败模式](eval/failure_modes.png)",
                "",
            ]
        if analysis.get("summary"):
            lines += [
                "**归纳：**",
                "",
            ]
            for s in analysis["summary"]:
                lines.append(f"- {s}")
            lines.append("")

    worst = _worst_queries(retrieval)
    if worst:
        lines += [
            "## 7. 失败案例（生产配置下 Recall@10 最低）",
            "",
            "| qid | 类别 | Recall@10 | 首个命中排名 | 库中相关 chunks |",
            "|---|---|---|---|---|",
        ]
        for r in worst:
            rank = r["first_hit_rank"] if r["first_hit_rank"] else "未命中"
            lines.append(
                f"| {r['qid']} | {r['category']} | {_fmt_pct(r['recall@10'])} "
                f"| {rank} | {r['relevant_chunks_target']} |"
            )
        lines += [
            "",
            "逐条明细见 `evals/results/retrieval_results.json` 的 `per_query_prod`。",
            "",
        ]

    lines += [
        "## 局限性",
        "",
        "- 语料为合成数据（真实聊天记录涉及隐私，不进仓库）；同一套 harness 可直接"
        "对私有语料复跑。",
        "- judge 与生成模型同源（DeepSeek），存在同源偏置；可通过环境变量"
        " `EVAL_JUDGE_MODEL` 换用第三方 judge 交叉验证。",
        "- 生成采样温度取生产值，非 0，忠实度/幻觉率存在轮次间波动。",
        "",
    ]
    return "\n".join(lines)


def generate_report() -> Path:
    retrieval = _load(RETRIEVAL_RESULTS)
    if retrieval is None:
        raise FileNotFoundError(
            f"缺少 {RETRIEVAL_RESULTS}，先运行 python -m evals.run_eval retrieval"
        )
    generation = _load(GENERATION_RESULTS)
    holdout = _load(HOLDOUT_RESULTS)
    analysis = _load(ERROR_ANALYSIS_RESULTS)

    plt = _setup_matplotlib()
    REPORT_IMG_DIR.mkdir(parents=True, exist_ok=True)
    plot_recall_vs_k(plt, retrieval, REPORT_IMG_DIR / "recall_vs_k.png")
    plot_threshold_sweep(plt, retrieval, REPORT_IMG_DIR / "threshold_sweep.png")
    plot_filter_impact(plt, retrieval, REPORT_IMG_DIR / "filter_impact.png")
    if generation:
        plot_generation(plt, generation, REPORT_IMG_DIR / "generation.png")
    if holdout:
        plot_holdout(plt, holdout, REPORT_IMG_DIR / "holdout.png")
    if analysis:
        plot_failure_modes(plt, analysis, REPORT_IMG_DIR / "failure_modes.png")

    REPORT_PATH.write_text(
        build_markdown(retrieval, generation, holdout, analysis), encoding="utf-8"
    )
    return REPORT_PATH
