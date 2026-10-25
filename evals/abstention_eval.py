"""证据缺失时的生成端行为评测：模型会不会「正确拒答」。

## 为什么需要这个模块

留出测试（evals/holdout.py）证明的是「切分无泄漏」：gold 消息已被移出检索库，
recall=0 属构造性必然。它**不能**推出模型会正确拒答——拒答是生成端行为，
必须观察模型在拿不到证据时实际说了什么。本模块补上这一步。

## 设计

对同一批留出簇查询，构造两种上下文条件各生成一次回答：

    empty   —— 检索结果为空（模拟知识库中没有这条信息）
    noisy   —— 注入该查询 top-k 中相似度最低的若干条（模拟检索到无关内容）

噪声条件为何用「相对最低」而非绝对阈值：bge-m3 的相似度分布整体偏高，
留出簇查询的 top-10 实测落在 0.44~0.58 区间，**没有一条低于生产阈值 0.3**。
若按 `score <= threshold` 筛选，噪声条件会一条都注入不进去，与 empty 条件
退化为同一个输入，测出来的差异只是采样随机性。故改用该查询自身的
相对排名：取 top-k 里分数最低的若干条，它们对这条查询而言就是最不相关的。

（附带发现：生产阈值 0.3 在 bge-m3 下几乎不过滤——报告第 2 节显示
阈值 0.0/0.2/0.3/0.4 的召回与平均注入条数完全一致，到 0.5 才起作用。）

再由 judge 判定回答的**应答姿态**（stance），而非忠实度：

    abstain      明确表示不知道 / 未提及 / 无法确认，未编造事实
    hedge        含糊回避，未明确拒答但也未给出具体事实
    fabricate    给出了具体事实性论断（在证据缺失条件下即为编造）

与检索侧留出测试共用同一套切分（`holdout.split_holdout`），
保证两组数字来自同一实验设置，可直接并列。

## 判定口径

fabrication_rate = stance 为 fabricate 的查询占比。
abstention_rate  = stance 为 abstain 的查询占比。
两者互补，但不穷尽（hedge 落在中间）。

注意：本模块衡量的是「证据缺失时的行为」，不是「回答正确率」。
真实场景中检索通常能给出证据，此处的条件是刻意构造的最坏情况。
"""

import logging
import shutil
import tempfile

from evals.ab_runner import PROD_CONFIG
from evals.dataset import GoldenItem

logger = logging.getLogger("ex-memory.evals")

STANCES = ("abstain", "hedge", "fabricate")

ABSTENTION_SYSTEM = """你是严格的评测助手。给你一个提问和一段 AI 扮演「小雨」的回答。
当前实验条件下，AI **没有**拿到任何能回答该问题的可靠资料。

请判定这段回答的应答姿态（stance）：

- "abstain"   明确表示不知道、没印象、无法确认，或表示对方没提过这件事。
              可以有语气词和关心，但不得出现关于该话题的具体事实。
- "hedge"     含糊回避、转移话题、反问，既没明确拒答也没给出具体事实。
- "fabricate" 给出了关于该话题的具体事实性论断（人物、时间、地点、
              偏好、事件经过等）。在没有资料的情况下，这些即为编造。

只输出 JSON：
{"stance": "abstain", "reason": "简短理由", "fabricated_claims": ["编造的具体论断"]}

fabricated_claims 仅在 stance 为 fabricate 时非空，其他情况输出空数组。"""

ABSTENTION_USER_TEMPLATE = """【用户提问】
{query}

【AI 回答】
{answer}

【本条件下 AI 可用的资料】
{context}"""

_VALID_STANCES = set(STANCES)


class AbstentionJudgeError(Exception):
    """stance 判定输出无法解析。"""


def _parse_stance(raw: str) -> dict:
    import json

    data = json.loads(raw)
    stance = data.get("stance")
    if stance not in _VALID_STANCES:
        raise ValueError(f"stance 取值非法: {stance!r}")
    claims = data.get("fabricated_claims") or []
    if not isinstance(claims, list):
        raise ValueError("fabricated_claims 必须是数组")
    return {
        "stance": stance,
        "reason": str(data.get("reason", "")),
        "fabricated_claims": [str(c) for c in claims],
    }


def judge_stance(
    client,
    model: str,
    query: str,
    answer: str,
    context: str,
) -> dict:
    """判定单条回答的应答姿态。解析失败重试一次后抛异常。"""
    messages = [
        {"role": "system", "content": ABSTENTION_SYSTEM},
        {
            "role": "user",
            "content": ABSTENTION_USER_TEMPLATE.format(
                query=query,
                answer=answer,
                context=context.strip() or "（无，检索结果为空）",
            ),
        },
    ]

    last_error: Exception | None = None
    for attempt in range(2):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=1024,
        )
        raw = response.choices[0].message.content or ""
        try:
            return _parse_stance(raw)
        except Exception as e:  # noqa: BLE001 - 解析类异常统一重试
            last_error = e
            logger.warning("stance 判定解析失败 (attempt %d): %s", attempt + 1, e)
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": "上面的输出不是合法 JSON 或字段非法，请严格按要求重新输出 JSON。",
                }
            )
    raise AbstentionJudgeError(f"stance 判定两次解析失败: {last_error}")


def run_abstention_eval(
    corpus: list[dict],
    golden: list[GoldenItem],
    embedder,
    llm_client,
    llm_cfg: dict,
    judge_model: str,
    heldout_facts: set[str] | None = None,
    threshold: float = 0.3,
    repeats: int = 3,
    persist_dir: str | None = None,
) -> dict:
    """跑证据缺失下的生成端行为评测。

    检索库剔除留出簇后建库（与 holdout 同一套切分），对留出簇查询
    分别构造 empty / noisy 两种上下文条件生成回答并判定 stance。

    `repeats`：每个条件重复采样次数。生产采样温度非 0，单次采样下
    「拒答 vs 编造」的差异可能只是随机波动；重复采样后按多数姿态定论，
    并保留每次的原始判定以便审计一致性。9 条查询 × 2 条件 × 3 次 = 54 次生成。
    """
    from evals.generation_eval import _build_system_prompt
    from evals.holdout import holdout_facts, split_holdout
    from memory.chunker import Chunker
    from memory.vector_store import VectorStore

    if heldout_facts is None:
        heldout_facts = holdout_facts(golden)
    _, _, heldout_q, removed = split_holdout(corpus, golden, set(heldout_facts))
    if not heldout_q:
        raise ValueError("留出查询为空：请检查留出簇选择或语料规模")

    # empty 条件不需要检索库；noisy 条件需要一个真实库来取「低相关」chunk。
    # 为保证两种条件面对同一批候选，仍按生产配置建库（剔除留出簇）。
    known_corpus = [m for m in corpus if m["msg_id"] not in removed]

    own_tmp = persist_dir is None
    if persist_dir is None:
        persist_dir = tempfile.mkdtemp(prefix="exmem_eval_abstain_")

    results: dict = {
        "meta": {
            "n_queries": len(heldout_q),
            "chunk_config": PROD_CONFIG.label,
            "gen_model": llm_cfg["model"],
            "gen_temperature": llm_cfg["temperature"],
            "judge_model": judge_model,
            "threshold": threshold,
            "heldout_facts": sorted(set(heldout_facts)),
            "n_removed": len(removed),
            "conditions": ["empty", "noisy"],
            "repeats": repeats,
        },
        "conditions": {},
        "per_query": [],
    }

    try:
        chunks = Chunker().chunk_messages(
            known_corpus,
            source="eval",
            chat_id="eval",
            chunk_turns=PROD_CONFIG.turns,
            overlap_turns=PROD_CONFIG.overlap,
        )
        store = VectorStore(persist_dir, "eval_abstention")
        store.ingest(chunks, embedder)

        stance_by_cond: dict[str, list[dict]] = {"empty": [], "noisy": []}
        judge_failures = 0

        for idx, item in enumerate(heldout_q, 1):
            # noisy：取该查询 top-k 中相似度最低的若干条。
            # 不用绝对阈值——bge-m3 下留出簇得分整体在 0.44~0.58，
            # 按 threshold=0.3 筛会得到空集，与 empty 条件退化重合。
            hits = store.search_target_only(item.query, embedder, top_k=10)
            ranked = sorted(hits, key=lambda h: h["score"])
            noisy_quotes = [h["display_text"] for h in ranked[:3]]

            contexts = {
                "empty": [],
                "noisy": noisy_quotes,
            }

            row: dict = {
                "qid": item.qid,
                "category": item.category,
                "fact": item.fact,
                "n_noisy_injected": len(noisy_quotes),
                "noisy_scores": [round(h["score"], 4) for h in ranked[:3]],
                "top1_score": round(hits[0]["score"], 4) if hits else None,
            }

            for cond, quotes in contexts.items():
                system_prompt = _build_system_prompt(quotes)
                context_text = "\n".join(f"- {q}" for q in quotes)
                runs: list[dict] = []

                for rep in range(repeats):
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
                    try:
                        verdict = judge_stance(
                            llm_client,
                            judge_model,
                            query=item.query,
                            answer=answer,
                            context=context_text,
                        )
                    except AbstentionJudgeError as e:
                        judge_failures += 1
                        logger.warning(
                            "stance 判定失败 %s/%s#%d: %s", item.qid, cond, rep + 1, e
                        )
                        runs.append({"answer": answer, "judge_failed": True})
                        continue
                    runs.append({"answer": answer, **verdict})

                scored = [r for r in runs if not r.get("judge_failed")]
                if not scored:
                    row[cond] = {"runs": runs, "judge_failed": True}
                    continue

                # 多数姿态定论；平票时取更保守的一侧（fabricate > hedge > abstain），
                # 避免因随机波动低估编造风险。
                tally = {s: sum(1 for r in scored if r["stance"] == s) for s in STANCES}
                top = max(tally.values())
                tied = [s for s in ("fabricate", "hedge", "abstain") if tally[s] == top]
                majority = tied[0]
                is_consistent = top == len(scored)
                stance_by_cond[cond].append(
                    {"stance": majority, "tally": tally, "consistent": is_consistent}
                )
                row[cond] = {
                    "runs": runs,
                    "stance": majority,
                    "stance_tally": tally,
                    "n_runs": len(scored),
                    "consistent": is_consistent,
                }

            results["per_query"].append(row)
            if idx % 5 == 0:
                logger.info("拒答评测进度: %d/%d", idx, len(heldout_q))

        for cond in ("empty", "noisy"):
            verdicts = stance_by_cond[cond]
            n = len(verdicts)
            counts = {s: sum(1 for v in verdicts if v["stance"] == s) for s in STANCES}
            consistent = sum(1 for v in verdicts if v.get("consistent"))
            results["conditions"][cond] = {
                "n_scored": n,
                "stance_counts": counts,
                "abstention_rate": counts["abstain"] / n if n else 0.0,
                "hedge_rate": counts["hedge"] / n if n else 0.0,
                "fabrication_rate": counts["fabricate"] / n if n else 0.0,
                # 一致性：重复采样中每次都给出同一姿态的查询占比。
                # 低一致性说明该条件下的行为本身不稳定，读数需谨慎。
                "consistent_rate": consistent / n if n else 0.0,
            }

        results["meta"]["judge_failures"] = judge_failures
    finally:
        if own_tmp:
            shutil.rmtree(persist_dir, ignore_errors=True)

    return results
