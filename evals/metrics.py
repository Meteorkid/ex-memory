"""手写评测指标：检索（Recall@K / Precision@K / MRR / nDCG@K）与生成（忠实度 / 幻觉率）。

设计要点：
- 全部纯函数，单条查询级别计算，聚合由调用方完成，方便单测与逐条审计。
- 检索结果以「每个 chunk 覆盖的消息 ID 集合」表示，与具体分块策略解耦——
  chunk ID 会随分块参数变化，消息级 ground truth 才能横向对比不同分块配置。
"""

import math


def recall_at_k(chunk_msg_ids: list[set[int]], gold_msg_ids: set[int], k: int) -> float:
    """消息级 Recall@K：top-K chunks 合计覆盖到的黄金消息占比。

    Args:
        chunk_msg_ids: 按检索排名排列，每个元素是该 chunk 覆盖的消息 ID 集合
        gold_msg_ids: 该查询标注的相关消息 ID
        k: 截断位置
    """
    if not gold_msg_ids:
        raise ValueError("gold_msg_ids 不能为空")
    if k <= 0:
        raise ValueError("k 必须为正数")
    covered: set[int] = set()
    for ids in chunk_msg_ids[:k]:
        covered |= ids & gold_msg_ids
    return len(covered) / len(gold_msg_ids)


def precision_at_k(
    chunk_msg_ids: list[set[int]], gold_msg_ids: set[int], k: int
) -> float:
    """Precision@K：top-K 中命中（覆盖至少一条黄金消息）的 chunk 占比，分母固定为 k。"""
    if k <= 0:
        raise ValueError("k 必须为正数")
    hits = sum(1 for ids in chunk_msg_ids[:k] if ids & gold_msg_ids)
    return hits / k


def mrr(chunk_msg_ids: list[set[int]], gold_msg_ids: set[int]) -> float:
    """Mean Reciprocal Rank 的单查询项：第一个命中 chunk 的倒数排名，未命中为 0。"""
    for i, ids in enumerate(chunk_msg_ids):
        if ids & gold_msg_ids:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(
    chunk_msg_ids: list[set[int]],
    gold_msg_ids: set[int],
    k: int,
    total_relevant: int,
) -> float:
    """二值相关性 nDCG@K。

    Args:
        total_relevant: 当前分块配置下，整个库中与该查询相关的 chunk 总数，
            用于计算理想 DCG（由 runner 扫描全部 chunks 得到）。
    """
    if k <= 0:
        raise ValueError("k 必须为正数")
    if total_relevant <= 0:
        return 0.0
    dcg = sum(
        1.0 / math.log2(i + 2)
        for i, ids in enumerate(chunk_msg_ids[:k])
        if ids & gold_msg_ids
    )
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(k, total_relevant)))
    return dcg / idcg


def hit_at_k(chunk_msg_ids: list[set[int]], gold_msg_ids: set[int], k: int) -> float:
    """Hit@K：top-K 内是否至少命中一条黄金消息（0/1）。"""
    if k <= 0:
        raise ValueError("k 必须为正数")
    return 1.0 if any(ids & gold_msg_ids for ids in chunk_msg_ids[:k]) else 0.0


# --- 生成指标 ---


def faithfulness_score(claims: list[dict]) -> float | None:
    """单条回答的忠实度：被检索上下文支持的论断占比。

    Args:
        claims: judge 输出的论断列表，每项含 supported: bool
    Returns:
        无事实性论断（纯寒暄）时返回 None，调用方应将其排除在分母外。
    """
    if not claims:
        return None
    supported = sum(1 for c in claims if c.get("supported"))
    return supported / len(claims)


def aggregate_generation(per_answer_claims: list[list[dict]]) -> dict:
    """聚合生成指标。

    Returns:
        faithfulness: 有论断回答的平均忠实度
        hallucination_rate: 含至少一条无依据论断的回答占比（分母为有论断的回答）
        n_scored: 参与计分的回答数
        n_no_claim: 无事实性论断被排除的回答数
    """
    scores = []
    hallucinated = 0
    n_no_claim = 0
    for claims in per_answer_claims:
        score = faithfulness_score(claims)
        if score is None:
            n_no_claim += 1
            continue
        scores.append(score)
        if any(not c.get("supported") for c in claims):
            hallucinated += 1
    n_scored = len(scores)
    return {
        "faithfulness": sum(scores) / n_scored if n_scored else 0.0,
        "hallucination_rate": hallucinated / n_scored if n_scored else 0.0,
        "n_scored": n_scored,
        "n_no_claim": n_no_claim,
    }


def aggregate_gold_consistency(per_answer_claims: list[list[dict]]) -> dict:
    """按黄金事实聚合幻觉指标。

    每条论断的 gold 取值：
        support    - 与黄金事实一致
        contradict - 与黄金事实矛盾
        absent     - 黄金事实中不存在

    **contradict 与 absent 必须分开看，把两者合成一个幻觉率会看不见真问题。**
    实测 135 条论断里 contradict 只有 3 条（记错了窗帘颜色、记错了蛋糕口味），
    absent 有 10 条，而那 10 条长这样：「对不起啦」「再辣我可扛不住」「不然
    十二点就困了」——是道歉、是主观感受、是把一件真事说得更像话。合并统计时
    严格口径给出 17.5%，其中真正的事实错误只占 3.5%。

    这个区分对本产品尤其要紧：一个只肯说可核验事实的前任，听起来像数据库。
    absent 不该压到 0，压到 0 就是把人味优化掉了；该压到 0 的是 contradict。

    Returns:
        hallucination_rate: 含至少一条 contradict/absent 论断的回答占比（严格口径，
            保留是为了与历史数据可比）
        contradiction_rate: 含至少一条 contradict 论断的回答占比——真正的事实错误
        unsupported_rate: 只含 absent、无 contradict 的回答占比——未被黄金事实
            覆盖的发挥，本产品预期非零
        claim_accuracy: 全部论断中 support 的占比
        n_scored / n_no_claim: 同 aggregate_generation
    """
    n_scored = 0
    n_no_claim = 0
    hallucinated = 0
    contradicted = 0
    unsupported_only = 0
    total_claims = 0
    supported_claims = 0
    for claims in per_answer_claims:
        if not claims:
            n_no_claim += 1
            continue
        n_scored += 1
        total_claims += len(claims)
        supported_claims += sum(1 for c in claims if c.get("gold") == "support")
        has_contradict = any(c.get("gold") == "contradict" for c in claims)
        has_absent = any(c.get("gold") == "absent" for c in claims)
        if has_contradict or has_absent:
            hallucinated += 1
        if has_contradict:
            contradicted += 1
        elif has_absent:
            unsupported_only += 1
    return {
        "hallucination_rate": hallucinated / n_scored if n_scored else 0.0,
        "contradiction_rate": contradicted / n_scored if n_scored else 0.0,
        "unsupported_rate": unsupported_only / n_scored if n_scored else 0.0,
        "claim_accuracy": supported_claims / total_claims if total_claims else 0.0,
        "n_scored": n_scored,
        "n_no_claim": n_no_claim,
    }


def mean(values: list[float]) -> float:
    """算术平均，空列表返回 0.0。"""
    return sum(values) / len(values) if values else 0.0
