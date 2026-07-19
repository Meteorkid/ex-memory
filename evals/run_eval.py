"""评测 CLI 入口。

用法：
    python -m evals.run_eval retrieval [--mock] [--limit N]
    python -m evals.run_eval generation [--limit N] [--threshold 0.3]
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
from evals.report import RESULTS_DIR, RETRIEVAL_RESULTS, GENERATION_RESULTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ex-memory.evals")


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
        cmd_report(args)

    p_all.set_defaults(func=_run_all)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
