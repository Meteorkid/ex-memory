"""检索 A/B 评测：分块策略 × 目标过滤 × 相似度阈值。

复用生产链路的 Chunker / VectorStore / Embedder，每组分块配置建独立的
临时 ChromaDB collection，跑全部 golden queries 并计算消息级指标。

chunk → 消息映射：chunk metadata 携带 start_ts/end_ts，语料 timestamp
严格递增且唯一，因此 [start_ts, end_ts] 可还原为连续的 msg_id 区间。
"""

import hashlib
import logging
import math
import shutil
import tempfile
from dataclasses import dataclass

from evals import metrics
from evals.dataset import GoldenItem

logger = logging.getLogger("ex-memory.evals")

K_VALUES = [1, 3, 5, 10]
MAX_TOP_K = 10  # 与生产 DEFAULT_TOP_K 一致
THRESHOLDS = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6]


@dataclass(frozen=True)
class ChunkConfig:
    turns: int
    overlap: int

    @property
    def label(self) -> str:
        return f"turns{self.turns}_overlap{self.overlap}"


# (5,1) 为当前生产配置 CHUNK_TURNS=5 / CHUNK_OVERLAP=1
CHUNK_CONFIGS = [
    ChunkConfig(3, 0),
    ChunkConfig(3, 1),
    ChunkConfig(5, 1),
    ChunkConfig(8, 2),
    ChunkConfig(10, 2),
]
PROD_CONFIG = ChunkConfig(5, 1)


class CachingEmbedder:
    """包装真实 Embedder，按文本缓存向量：查询向量跨分块配置只算一次。"""

    def __init__(self, base):
        self.base = base
        self._cache: dict[str, list[float]] = {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in texts if t not in self._cache]
        if missing:
            # 去重后再请求
            unique = list(dict.fromkeys(missing))
            vectors = self.base.embed(unique)
            self._cache.update(zip(unique, vectors))
        return [self._cache[t] for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class MockEmbedder:
    """离线用的确定性伪向量：字符 bigram 哈希到固定维度并归一化。

    只提供粗糙的词面相似度，用于单测与无 API key 时的冒烟运行，
    检索质量数字不具参考意义。
    """

    DIM = 256

    def _vectorize(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        for i in range(len(text) - 1):
            bigram = text[i : i + 2]
            h = int(hashlib.md5(bigram.encode()).hexdigest()[:8], 16)
            vec[h % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorize(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self._vectorize(text)


def chunk_msg_range(chunk_meta: dict, ts_to_id: dict[str, int]) -> set[int]:
    """由 chunk 的 start_ts/end_ts 还原其覆盖的 msg_id 集合。"""
    start = ts_to_id[chunk_meta["start_ts"]]
    end = ts_to_id[chunk_meta["end_ts"]]
    return set(range(start, end + 1))


def _rank_metrics(
    ranked_msg_ids: list[set[int]], gold: set[int], total_relevant: int
) -> dict:
    out = {}
    for k in K_VALUES:
        out[f"recall@{k}"] = metrics.recall_at_k(ranked_msg_ids, gold, k)
        out[f"precision@{k}"] = metrics.precision_at_k(ranked_msg_ids, gold, k)
        out[f"hit@{k}"] = metrics.hit_at_k(ranked_msg_ids, gold, k)
        out[f"ndcg@{k}"] = metrics.ndcg_at_k(ranked_msg_ids, gold, k, total_relevant)
    out["mrr"] = metrics.mrr(ranked_msg_ids, gold)
    return out


def _aggregate(per_query: list[dict]) -> dict:
    keys = per_query[0].keys()
    return {key: metrics.mean([q[key] for q in per_query]) for key in keys}


def run_retrieval_eval(
    corpus: list[dict],
    golden: list[GoldenItem],
    embedder,
    chunk_configs: list[ChunkConfig] = CHUNK_CONFIGS,
    persist_dir: str | None = None,
) -> dict:
    """跑完整检索 A/B，返回可 JSON 序列化的结果字典。"""
    from memory.chunker import Chunker
    from memory.vector_store import VectorStore

    ts_to_id = {m["timestamp"]: m["msg_id"] for m in corpus}
    own_tmp = persist_dir is None
    if persist_dir is None:
        persist_dir = tempfile.mkdtemp(prefix="exmem_eval_chroma_")

    chunker = Chunker()
    results: dict = {
        "meta": {
            "corpus_size": len(corpus),
            "n_queries": len(golden),
            "k_values": K_VALUES,
            "thresholds": THRESHOLDS,
            "max_top_k": MAX_TOP_K,
            # 记录 embedding 模型名；MockEmbedder 等无 model 属性时回退到类名
            "embedder": getattr(
                getattr(embedder, "base", embedder),
                "model",
                type(getattr(embedder, "base", embedder)).__name__,
            ),
        },
        "configs": [],
        "per_query_prod": [],
    }

    try:
        for cfg in chunk_configs:
            chunks = chunker.chunk_messages(
                corpus,
                source="eval",
                chat_id="eval",
                chunk_turns=cfg.turns,
                overlap_turns=cfg.overlap,
            )
            chunk_ranges = {
                c["id"]: chunk_msg_range(c["metadata"], ts_to_id) for c in chunks
            }
            target_chunk_ids = {
                c["id"] for c in chunks if c["metadata"]["dominant_speaker"] == "target"
            }

            store = VectorStore(persist_dir, f"eval_{cfg.label}")
            store.ingest(chunks, embedder)
            logger.info(
                "[%s] %d chunks 入库（target 占 %d）",
                cfg.label,
                len(chunks),
                len(target_chunk_ids),
            )

            per_query_all: list[dict] = []
            per_query_target: list[dict] = []
            sweep_acc: dict[float, list[dict]] = {t: [] for t in THRESHOLDS}

            for item in golden:
                gold = set(item.gold_msg_ids)
                # 该配置下库中相关 chunk 总数（nDCG 理想值用）
                relevant_all = sum(1 for ids in chunk_ranges.values() if ids & gold)
                relevant_target = sum(
                    1
                    for cid, ids in chunk_ranges.items()
                    if ids & gold and cid in target_chunk_ids
                )

                hits_all = store.search(item.query, embedder, top_k=MAX_TOP_K)
                hits_target = store.search_target_only(
                    item.query, embedder, top_k=MAX_TOP_K
                )

                def _to_ranked(hits: list[dict]) -> list[set[int]]:
                    return [chunk_msg_range(h["metadata"], ts_to_id) for h in hits]

                ranked_all = _to_ranked(hits_all)
                ranked_target = _to_ranked(hits_target)

                q_all = _rank_metrics(ranked_all, gold, relevant_all)
                q_target = _rank_metrics(ranked_target, gold, relevant_target)
                per_query_all.append(q_all)
                per_query_target.append(q_target)

                # 阈值扫描：生产路径（target 过滤 + score > threshold）
                scores = [h["score"] for h in hits_target]
                for t in THRESHOLDS:
                    kept = [ids for ids, s in zip(ranked_target, scores) if s > t]
                    covered: set[int] = set()
                    for ids in kept:
                        covered |= ids & gold
                    sweep_acc[t].append(
                        {
                            "recall": len(covered) / len(gold),
                            "kept": float(len(kept)),
                            "precision_kept": (
                                sum(1 for ids in kept if ids & gold) / len(kept)
                                if kept
                                else 0.0
                            ),
                        }
                    )

                if cfg == PROD_CONFIG:
                    first_rank = next(
                        (i + 1 for i, ids in enumerate(ranked_target) if ids & gold),
                        None,
                    )
                    results["per_query_prod"].append(
                        {
                            "qid": item.qid,
                            "category": item.category,
                            "recall@5": q_target["recall@5"],
                            "recall@10": q_target["recall@10"],
                            "mrr": q_target["mrr"],
                            "first_hit_rank": first_rank,
                            "top1_score": round(scores[0], 4) if scores else None,
                            "relevant_chunks_target": relevant_target,
                        }
                    )

            results["configs"].append(
                {
                    "label": cfg.label,
                    "turns": cfg.turns,
                    "overlap": cfg.overlap,
                    "n_chunks": len(chunks),
                    "n_target_chunks": len(target_chunk_ids),
                    "unfiltered": _aggregate(per_query_all),
                    "target_only": _aggregate(per_query_target),
                    "threshold_sweep": [
                        {
                            "threshold": t,
                            "recall": metrics.mean([r["recall"] for r in rows]),
                            "avg_kept": metrics.mean([r["kept"] for r in rows]),
                            "precision_kept": metrics.mean(
                                [r["precision_kept"] for r in rows]
                            ),
                        }
                        for t, rows in sweep_acc.items()
                    ],
                }
            )
            logger.info(
                "[%s] 完成: target_only recall@5=%.3f mrr=%.3f",
                cfg.label,
                results["configs"][-1]["target_only"]["recall@5"],
                results["configs"][-1]["target_only"]["mrr"],
            )
    finally:
        if own_tmp:
            shutil.rmtree(persist_dir, ignore_errors=True)

    return results
