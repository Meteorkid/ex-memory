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
    fig, ax = plt.subplots(figsize=(7, 4.5))
    k_values = retrieval["meta"]["k_values"]
    for cfg in retrieval["configs"]:
        recalls = [cfg["target_only"][f"recall@{k}"] for k in k_values]
        style = "-o" if cfg["label"] == "turns5_overlap1" else "--s"
        label = f"{cfg['turns']}轮/重叠{cfg['overlap']}"
        if cfg["label"] == "turns5_overlap1":
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
    cfg = next(c for c in retrieval["configs"] if c["label"] == "turns5_overlap1")
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

    fig.suptitle("阈值扫描：召回 vs 上下文注入量（生产分块 5轮/重叠1）")
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


def _worst_queries(retrieval: dict, n: int = 5) -> list[dict]:
    rows = [r for r in retrieval.get("per_query_prod", []) if r["recall@10"] < 1.0]
    return sorted(rows, key=lambda r: (r["recall@10"], r["mrr"]))[:n]


def build_markdown(retrieval: dict, generation: dict | None) -> str:
    meta = retrieval["meta"]
    prod = next(c for c in retrieval["configs"] if c["label"] == "turns5_overlap1")
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
        mark = "（生产）" if cfg["label"] == "turns5_overlap1" else ""
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

    worst = _worst_queries(retrieval)
    if worst:
        lines += [
            "## 5. 失败案例（生产配置下 Recall@10 最低）",
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

    plt = _setup_matplotlib()
    REPORT_IMG_DIR.mkdir(parents=True, exist_ok=True)
    plot_recall_vs_k(plt, retrieval, REPORT_IMG_DIR / "recall_vs_k.png")
    plot_threshold_sweep(plt, retrieval, REPORT_IMG_DIR / "threshold_sweep.png")
    plot_filter_impact(plt, retrieval, REPORT_IMG_DIR / "filter_impact.png")
    if generation:
        plot_generation(plt, generation, REPORT_IMG_DIR / "generation.png")

    REPORT_PATH.write_text(build_markdown(retrieval, generation), encoding="utf-8")
    return REPORT_PATH
