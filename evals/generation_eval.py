"""生成端评测：RAG vs 无 RAG 的忠实度与幻觉率对比。

生成侧完整复刻生产路径：生产分块配置入库 → search_target_only(top_k=10)
→ score > threshold 过滤 → 与 engine._build_system_prompt 相同的「潜意识层」
格式注入上下文 → 生产采样参数生成回答；再由 judge 逐条论断打分。
"""

import logging
import random
import tempfile
import shutil

from evals import metrics
from evals.ab_runner import PROD_CONFIG
from evals.build_corpus import PERSONA_PROMPT
from evals.dataset import GoldenItem
from evals.judge import JudgeError, judge_answer

logger = logging.getLogger("ex-memory.evals")

SAMPLE_SEED = 42

# 与 core/engine.py _build_system_prompt 的 RAG 注入段保持一致的格式
RAG_CONTEXT_TEMPLATE = (
    "\n---\n## 潜意识层 — ta 在类似场景下真实说过的话\n"
    "以下是从聊天记录中检索到的 ta 的原话，作为你回复的语气锚点：\n"
    "{quotes}\n"
    "请以这些原话的语气、标点习惯、断句方式为参考来回复。\n"
)


def _build_system_prompt(context_quotes: list[str]) -> str:
    prompt = PERSONA_PROMPT
    if context_quotes:
        quotes = "\n".join(f"- {q}" for q in context_quotes)
        prompt += RAG_CONTEXT_TEMPLATE.format(quotes=quotes)
    return prompt


def sample_golden(golden: list[GoldenItem], limit: int) -> list[GoldenItem]:
    """确定性抽样：每个事实簇最多取一条查询，再随机补足，避免同簇重复。"""
    if limit >= len(golden):
        return list(golden)
    rng = random.Random(SAMPLE_SEED)
    by_fid: dict[str, list[GoldenItem]] = {}
    for item in golden:
        fid = item.qid.rsplit("-q", 1)[0]
        by_fid.setdefault(fid, []).append(item)
    picked = [rng.choice(items) for items in by_fid.values()]
    if len(picked) > limit:
        picked = rng.sample(picked, limit)
    elif len(picked) < limit:
        rest = [g for g in golden if g not in picked]
        picked += rng.sample(rest, limit - len(picked))
    return sorted(picked, key=lambda g: g.qid)


def run_generation_eval(
    corpus: list[dict],
    golden: list[GoldenItem],
    embedder,
    llm_client,
    llm_cfg: dict,
    judge_model: str,
    limit: int = 60,
    threshold: float = 0.3,
    modes: tuple[str, ...] = ("rag", "no_rag"),
    persist_dir: str | None = None,
) -> dict:
    """跑生成评测，返回可 JSON 序列化的结果字典。"""
    from memory.chunker import Chunker
    from memory.vector_store import VectorStore

    items = sample_golden(golden, limit)
    own_tmp = persist_dir is None
    if own_tmp:
        persist_dir = tempfile.mkdtemp(prefix="exmem_eval_gen_")

    results: dict = {
        "meta": {
            "n_items": len(items),
            "threshold": threshold,
            "chunk_config": PROD_CONFIG.label,
            "gen_model": llm_cfg["model"],
            "gen_temperature": llm_cfg["temperature"],
            "judge_model": judge_model,
            "modes": list(modes),
        },
        "modes": {},
        "per_item": [],
    }

    try:
        chunks = Chunker().chunk_messages(
            corpus,
            source="eval",
            chat_id="eval",
            chunk_turns=PROD_CONFIG.turns,
            overlap_turns=PROD_CONFIG.overlap,
        )
        store = VectorStore(persist_dir, "eval_generation")
        store.ingest(chunks, embedder)

        claims_by_mode: dict[str, list[list[dict]]] = {m: [] for m in modes}
        judge_failures = 0

        for idx, item in enumerate(items, 1):
            hits = store.search_target_only(item.query, embedder, top_k=10)
            kept_quotes = [h["display_text"] for h in hits if h["score"] > threshold]
            gold_quotes = [corpus[mid]["content"] for mid in sorted(item.gold_msg_ids)]

            row: dict = {
                "qid": item.qid,
                "category": item.category,
                "n_context": len(kept_quotes),
            }

            for mode in modes:
                quotes = kept_quotes if mode == "rag" else []
                system_prompt = _build_system_prompt(quotes)
                response = llm_client.chat.completions.create(
                    model=llm_cfg["model"],
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": item.query},
                    ],
                    temperature=llm_cfg["temperature"],
                    top_p=llm_cfg["top_p"],
                    frequency_penalty=llm_cfg["frequency_penalty"],
                    max_tokens=512,
                )
                answer = response.choices[0].message.content or ""

                context_text = "\n".join(f"- {q}" for q in quotes)
                try:
                    claims = judge_answer(
                        llm_client,
                        judge_model,
                        query=item.query,
                        answer=answer,
                        context=context_text,
                        gold_fact=item.fact,
                        gold_quotes=gold_quotes,
                    )
                except JudgeError as e:
                    judge_failures += 1
                    logger.warning("judge 失败，跳过 %s/%s: %s", item.qid, mode, e)
                    row[mode] = {"answer": answer, "judge_failed": True}
                    continue

                claims_by_mode[mode].append(claims)
                row[mode] = {"answer": answer, "claims": claims}

            results["per_item"].append(row)
            if idx % 10 == 0:
                logger.info("生成评测进度: %d/%d", idx, len(items))

        for mode in modes:
            faith_input = [
                [{"supported": c["supported_by_context"]} for c in claims]
                for claims in claims_by_mode[mode]
            ]
            faith = metrics.aggregate_generation(faith_input)
            gold = metrics.aggregate_gold_consistency(claims_by_mode[mode])
            results["modes"][mode] = {
                # 忠实度：论断被检索上下文支持的比例（no_rag 模式无上下文，恒为 0）
                "faithfulness": faith["faithfulness"],
                "unfaithful_answer_rate": faith["hallucination_rate"],
                # 幻觉率：与黄金事实矛盾或凭空编造细节的回答占比
                "hallucination_rate": gold["hallucination_rate"],
                "claim_accuracy": gold["claim_accuracy"],
                "n_scored": gold["n_scored"],
                "n_no_claim": gold["n_no_claim"],
            }

        results["meta"]["judge_failures"] = judge_failures
    finally:
        if own_tmp:
            shutil.rmtree(persist_dir, ignore_errors=True)

    return results
