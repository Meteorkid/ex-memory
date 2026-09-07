"""事实簇级留出（holdout）评测：测『未见知识的检索泛化』。

常规评测（run_retrieval_eval）的 golden 查询与检索语料同源，无法证明跨分布泛化。
本模块把若干事实簇（fact）的 target 消息从语料剔除作为检索库，同一查询集
分两组对比：

    known:   gold 消息仍在库 → 期望保持正常召回
    heldout: gold 消息已移除 → 期望被判为『库外无相关』（低召回）

复用 ab_runner 的 Chunker / VectorStore / 指标，单配置（生产）跑一个库，控制成本。
注意：语料本身是合成数据，此处衡量的是『同分布下的未见事实』，不等于真实分布泛化。
"""

import logging
import random
import shutil
import tempfile

from evals import metrics
from evals.ab_runner import (
    MAX_TOP_K,
    PROD_CONFIG,
    chunk_msg_range,
)
from evals.dataset import GoldenItem

logger = logging.getLogger("ex-memory.evals")

# 默认留出抽样：避开占比最高的『偏好』，从经验类簇抽，代表更强。
DEFAULT_POOL_CATEGORIES = ("经历", "工作学校", "家庭朋友", "习惯怪癖", "健康约定")
METRIC_KEYS = (
    "recall@1",
    "recall@3",
    "recall@5",
    "recall@10",
    "precision@5",
    "hit@5",
    "mrr",
    "ndcg@10",
)


def _fact_gold_ids(golden: list[GoldenItem]) -> dict[str, set[int]]:
    """事实簇 → 该簇所有黄金消息 id（一条事实可命中多条 target 消息）。"""
    facts: dict[str, set[int]] = {}
    for g in golden:
        facts.setdefault(g.fact, set()).update(g.gold_msg_ids)
    return facts


def _fact_category(golden: list[GoldenItem]) -> dict[str, str]:
    return {g.fact: g.category for g in golden}


def holdout_facts(
    golden: list[GoldenItem],
    n: int = 3,
    seed: int = 7,
    pool_categories: tuple[str, ...] = DEFAULT_POOL_CATEGORIES,
) -> set[str]:
    """用固定种子采样 n 个事实簇作为留出。

    优先从 pool_categories 之外的... 从经验类抽取；池不足时回退到全部簇，
    保证 n 个无论语料规模如何都能取到。
    """
    facts = sorted({g.fact for g in golden})
    cat_of = _fact_category(golden)
    pool = [f for f in facts if cat_of[f] in pool_categories]
    if len(pool) < n:
        pool = facts
    rng = random.Random(seed)
    return set(rng.sample(pool, min(n, len(pool))))


def split_holdout(
    corpus: list[dict], golden: list[GoldenItem], heldout_facts: set[str]
) -> tuple[list[dict], list[GoldenItem], list[GoldenItem], set[int]]:
    """按事实簇留出切分。

    Returns:
        (known_corpus, known_queries, heldout_queries, removed_msg_ids)
        - known_corpus: 剔除留出簇 target 消息后的检索语料
        - known_queries: gold 消息全部仍留在库中的查询
        - heldout_queries: 至少引用一条被剔消息的查询
        - removed_msg_ids: 被从库中移除的消息 id
    """
    fact_gold = _fact_gold_ids(golden)
    removed: set[int] = set()
    for f in heldout_facts:
        removed |= fact_gold.get(f, set())

    known_corpus = [m for m in corpus if m["msg_id"] not in removed]

    known_q: list[GoldenItem] = []
    heldout_q: list[GoldenItem] = []
    for g in golden:
        if set(g.gold_msg_ids) & removed:
            heldout_q.append(g)
        else:
            known_q.append(g)
    return known_corpus, known_q, heldout_q, removed


def _query_metrics(
    ranked: list[set[int]], gold: set[int], relevant: int
) -> dict[str, float | str | None]:
    """计算单条查询的全部留出检索指标。"""
    return {
        "recall@1": metrics.recall_at_k(ranked, gold, 1),
        "recall@3": metrics.recall_at_k(ranked, gold, 3),
        "recall@5": metrics.recall_at_k(ranked, gold, 5),
        "recall@10": metrics.recall_at_k(ranked, gold, 10),
        "precision@5": metrics.precision_at_k(ranked, gold, 5),
        "hit@5": metrics.hit_at_k(ranked, gold, 5),
        "mrr": metrics.mrr(ranked, gold),
        "ndcg@10": metrics.ndcg_at_k(ranked, gold, 10, relevant),
    }


def run_holdout_eval(
    corpus: list[dict],
    golden: list[GoldenItem],
    embedder,
    heldout_facts: set[str] | None = None,
    persist_dir: str | None = None,
) -> dict:
    """跑单配置留出评测，返回 known / heldout 两组聚合指标与元信息。

    检索库只 ingest 剔除留出簇后的 known_corpus；ts_to_id 用完整 corpus 构建，
    保证 chunk 的 start_ts/end_ts 还原为与 golden 一致的全局 msg_id。
    """
    from memory.chunker import Chunker
    from memory.vector_store import VectorStore

    if heldout_facts is None:
        heldout_facts = holdout_facts(golden)
    known_corpus, known_q, heldout_q, removed = split_holdout(
        corpus, golden, set(heldout_facts)
    )
    if not known_corpus or not heldout_q or not known_q:
        raise ValueError(
            "留出切分得到空子集：请减小留出簇数量或检查语料规模"
        )

    # 库中实际存在的消息 id；剔除后 msg_id 不连续，连续 range 还原会误包含被剔
    # 消息，因此每个 chunk 的覆盖区间需与已知集求交。
    known_ids = {m["msg_id"] for m in known_corpus}
    ts_to_id = {m["timestamp"]: m["msg_id"] for m in corpus}

    own_tmp = persist_dir is None
    if persist_dir is None:
        persist_dir = tempfile.mkdtemp(prefix="exmem_eval_holdout_")

    try:
        chunker = Chunker()
        chunks = chunker.chunk_messages(
            known_corpus,
            source="eval",
            chat_id="eval",
            chunk_turns=PROD_CONFIG.turns,
            overlap_turns=PROD_CONFIG.overlap,
        )
        chunk_ranges = {
            c["id"]: chunk_msg_range(c["metadata"], ts_to_id) & known_ids
            for c in chunks
        }
        store = VectorStore(persist_dir, "eval_holdout")
        store.ingest(chunks, embedder)
        logger.info(
            "留存库 %d 条消息 / %d chunks（剔除 %d 条留出消息）",
            len(known_corpus),
            len(chunks),
            len(removed),
        )

        def _run_group(items: list[GoldenItem]) -> tuple[dict, list[dict]]:
            per_q: list[dict] = []
            for item in items:
                gold = set(item.gold_msg_ids)
                relevant = sum(1 for ids in chunk_ranges.values() if ids & gold)
                hits = store.search_target_only(
                    item.query, embedder, top_k=MAX_TOP_K
                )
                ranked = [
                    chunk_msg_range(h["metadata"], ts_to_id) & known_ids
                    for h in hits
                ]
                row = _query_metrics(ranked, gold, relevant)
                row["qid"] = item.qid
                row["top1_score"] = round(hits[0]["score"], 4) if hits else None
                per_q.append(row)
            agg = {
                k: metrics.mean([q[k] for q in per_q if q[k] is not None])
                for k in METRIC_KEYS
            }
            return agg, per_q

        known_agg, known_per = _run_group(known_q)
        heldout_agg, heldout_per = _run_group(heldout_q)
    finally:
        if own_tmp:
            shutil.rmtree(persist_dir, ignore_errors=True)

    return {
        "known": known_agg,
        "heldout": heldout_agg,
        "meta": {
            "config": PROD_CONFIG.label,
            "corpus_size": len(corpus),
            "known_corpus_size": len(known_corpus),
            "n_removed": len(removed),
            "n_known": len(known_q),
            "n_heldout": len(heldout_q),
            "heldout_facts": sorted(set(heldout_facts)),
            "embedder": getattr(
                getattr(embedder, "base", embedder),
                "model",
                type(getattr(embedder, "base", embedder)).__name__,
            ),
        },
    }