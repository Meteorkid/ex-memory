"""评测 CLI 入口。

用法：
    python -m evals.run_eval retrieval [--mock] [--limit N]
    python -m evals.run_eval generation [--limit N] [--threshold 0.3]
    python -m evals.run_eval holdout [--mock] [--facts N] [--seed N] [--fact F,...]
    python -m evals.run_eval error
    python -m evals.run_eval report
    python -m evals.run_eval all [--limit N]

--mock 使用离线伪向量（无 API key 冒烟用，数字不具参考意义）。
"""

import argparse
import json
import logging
import os
import sys

from evals.dataset import load_corpus, load_golden
from evals.report import (
    ERROR_ANALYSIS_RESULTS,
    GENERATION_RESULTS,
    HOLDOUT_RESULTS,
    RESULTS_DIR,
    RETRIEVAL_RESULTS,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ex-memory.evals")

def _load_json(path):
    if not path.exists():
        sys.exit(f"缺少 {path}，先运行生成它的子命令")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _make_embedder(mock: bool):
    from evals.ab_runner import CachingEmbedder, MockEmbedder

    if mock:
        return MockEmbedder()
    from config import get_embedding_config
    from memory.embedder import Embedder

    cfg = get_embedding_config()
    if not cfg["api_key"]:
        sys.exit(
            "EMBEDDING_API_KEY 未配置：真实评测需要 embedding 服务，或加 --mock 离线冒烟"
        )
    return CachingEmbedder(Embedder(cfg["api_key"], cfg["base_url"], cfg["model"]))


def _save(path, data: dict):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    logger.info("结果已写入 %s", path)


def cmd_retrieval(args):
    corpus = load_corpus()
    golden = load_golden(corpus=corpus)
    if args.limit:
        golden = golden[: args.limit]
    embedder = _make_embedder(args.mock)

    from evals.ab_runner import run_retrieval_eval

    results = run_retrieval_eval(corpus, golden, embedder)
    if args.mock:
        results["meta"]["mock"] = True
    _save(RETRIEVAL_RESULTS, results)
    for cfg in results["configs"]:
        t = cfg["target_only"]
        print(
            f"{cfg['label']:>18}: recall@5={t['recall@5']:.3f} "
            f"recall@10={t['recall@10']:.3f} mrr={t['mrr']:.3f}"
        )


def cmd_generation(args):
    corpus = load_corpus()
    golden = load_golden(corpus=corpus)
    embedder = _make_embedder(args.mock)

    from config import get_llm_client, get_llm_config
    from evals.generation_eval import run_generation_eval

    llm_cfg = get_llm_config()
    if not llm_cfg["api_key"]:
        sys.exit("LLM_API_KEY 未配置：生成评测需要 LLM 服务")
    judge_model = os.getenv("EVAL_JUDGE_MODEL", llm_cfg["model"])

    results = run_generation_eval(
        corpus,
        golden,
        embedder,
        llm_client=get_llm_client(),
        llm_cfg=llm_cfg,
        judge_model=judge_model,
        limit=args.limit or 60,
        threshold=args.threshold,
    )
    _save(GENERATION_RESULTS, results)
    for mode, m in results["modes"].items():
        print(
            f"{mode:>7}: faithfulness={m['faithfulness']:.3f} "
            f"hallucination={m['hallucination_rate']:.3f} n={m['n_scored']}"
        )


def cmd_holdout(args):
    from evals.holdout import holdout_facts, run_holdout_eval

    corpus = load_corpus()
    golden = load_golden(corpus=corpus)
    embedder = _make_embedder(args.mock)

    if args.fact:
        heldout = set(args.fact)
    else:
        heldout = holdout_facts(golden, n=args.facts, seed=args.seed)
    logger.info("留出事实簇：%s", "、".join(sorted(heldout)))

    results = run_holdout_eval(corpus, golden, embedder, heldout_facts=heldout)
    if args.mock:
        results["meta"]["mock"] = True
    _save(HOLDOUT_RESULTS, results)
    k = results["meta"]["n_known"]
    h = results["meta"]["n_heldout"]
    print(
        f"known({k}):   recall@10={results['known']['recall@10']:.3f} "
        f"mrr={results['known']['mrr']:.3f}"
    )
    print(
        f"heldout({h}): recall@10={results['heldout']['recall@10']:.3f} "
        f"mrr={results['heldout']['mrr']:.3f}"
    )


def cmd_error(_args):
    from evals.error_analysis import run_error_analysis

    retrieval = _load_json(RETRIEVAL_RESULTS)
    holdout = _load_json(HOLDOUT_RESULTS)
    results = run_error_analysis(retrieval, holdout)
    _save(ERROR_ANALYSIS_RESULTS, results)
    m = results["meta"]
    print(
        f"错误分析：{m['n_queries']} 条查询，{m['n_failed']} 条失败"
        f"（失败率 {m['fail_rate']:.1%}）"
    )
    for line in results["summary"]:
        print(f"  - {line}")


def cmd_report(_args):
    from evals.report import generate_report

    path = generate_report()
    print(f"报告已生成: {path}")


def main():
    parser = argparse.ArgumentParser(description="ex-memory RAG 评测")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ret = sub.add_parser("retrieval", help="检索 A/B：分块×过滤×阈值")
    p_ret.add_argument("--mock", action="store_true", help="离线伪向量冒烟")
    p_ret.add_argument("--limit", type=int, default=0, help="只跑前 N 条查询")
    p_ret.set_defaults(func=cmd_retrieval)

    p_gen = sub.add_parser("generation", help="生成评测：忠实度/幻觉率")
    p_gen.add_argument("--mock", action="store_true", help="检索侧用伪向量（调试）")
    p_gen.add_argument(
        "--limit", type=int, default=60, help="抽样条数（控制 API 成本）"
    )
    p_gen.add_argument("--threshold", type=float, default=0.3, help="RAG 相似度阈值")
    p_gen.set_defaults(func=cmd_generation)

    p_hold = sub.add_parser(
        "holdout", help="事实簇级留出：测未见知识的检索泛化"
    )
    p_hold.add_argument("--mock", action="store_true", help="离线伪向量冒烟")
    p_hold.add_argument("--facts", type=int, default=3, help="留出事实簇数量")
    p_hold.add_argument("--seed", type=int, default=7, help="抽样随机种子")
    p_hold.add_argument(
        "--fact",
        action="append",
        help="显式指定留出事实簇（可多次），覆盖自动抽样",
    )
    p_hold.set_defaults(func=cmd_holdout)

    p_err = sub.add_parser("error", help="错误分析：从检索/留出结果归因失败模式")
    p_err.set_defaults(func=cmd_error)

    p_rep = sub.add_parser("report", help="从已有结果生成图表与报告")
    p_rep.set_defaults(func=cmd_report)

    p_all = sub.add_parser("all", help="retrieval + generation + report")
    p_all.add_argument("--mock", action="store_true")
    p_all.add_argument("--limit", type=int, default=60)
    p_all.add_argument("--threshold", type=float, default=0.3)

    def _run_all(args):
        ret_args = argparse.Namespace(mock=args.mock, limit=0)
        cmd_retrieval(ret_args)
        gen_args = argparse.Namespace(
            mock=args.mock, limit=args.limit, threshold=args.threshold
        )
        cmd_generation(gen_args)
        hold_args = argparse.Namespace(mock=args.mock, facts=3, seed=7, fact=None)
        cmd_holdout(hold_args)
        cmd_error(argparse.Namespace())
        cmd_report(args)

    p_all.set_defaults(func=_run_all)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
