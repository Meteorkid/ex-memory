"""表达指纹：把「像不像 ta」变成可测量的数字（FR-060）。

现有 evals 评的是检索准不准、生成有没有编造事实，评不出**像不像这个人**。
这里补上：从语料里提取一组风格特征，用分布距离衡量生成文本与真人语料的
差异。

**刻意全部用纯统计，不依赖 LLM 判定**：
- 成本为零，可以在 CI 里每次跑；
- 结果确定性可复现，指标回归时能直接阻断发布；
- LLM judge 的口径会随模型版本漂移，不适合当门禁。

指标本身不判断「好不好」，只判断「像不像」。句子变长可能是文笔变好了，
但如果 ta 本人就说短句，那就是不像。
"""

import math
import re
from collections import Counter
from typing import Iterable, Optional

# 中文常见句末标点 + 英文
_SENTENCE_END = "。！？!?…"
_PUNCT = "，。！？、；：·…—～~,.!?;:"
_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff]")
# 微信里高频的语气词与拟声，是个人风格最稳定的部分之一
_FILLER_CANDIDATES = [
    "哈哈",
    "呵呵",
    "嗯",
    "啊",
    "呀",
    "吧",
    "呢",
    "哦",
    "噢",
    "唉",
    "嘛",
    "咯",
    "喔",
    "哇",
    "诶",
    "嘿",
    "嗷",
    "唔",
]

# 句长分箱：字符数
_LENGTH_BINS = [0, 3, 6, 10, 16, 25, 40, 70, 10**9]


def _bin_index(value: float, bins: list) -> int:
    for i in range(len(bins) - 1):
        if bins[i] <= value < bins[i + 1]:
            return i
    return len(bins) - 2


def _normalize(counter: Counter, keys: Iterable) -> list[float]:
    total = sum(counter.values())
    if total == 0:
        keys = list(keys)
        return [1.0 / len(keys)] * len(keys)
    return [counter.get(k, 0) / total for k in keys]


def _split_sentences(text: str) -> list[str]:
    parts, buf = [], []
    for ch in text:
        buf.append(ch)
        if ch in _SENTENCE_END:
            parts.append("".join(buf).strip())
            buf = []
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p]


def fingerprint(texts: list[str]) -> dict:
    """从一组文本提取表达指纹。

    输入应是**同一个人**说的话。混入对方的发言会把指纹拉平，测不出差异。
    """
    texts = [t for t in texts if t and t.strip()]
    if not texts:
        return {"sample_size": 0}

    length_bins: Counter = Counter()
    punct_counts: Counter = Counter()
    filler_counts: Counter = Counter()
    msg_len_bins: Counter = Counter()
    emoji_chars = 0
    total_chars = 0
    sentences_per_message: list[int] = []
    ends_without_punct = 0

    for text in texts:
        total_chars += len(text)
        emoji_chars += len(_EMOJI.findall(text))
        msg_len_bins[_bin_index(len(text), _LENGTH_BINS)] += 1

        sentences = _split_sentences(text)
        sentences_per_message.append(max(1, len(sentences)))
        for sentence in sentences:
            length_bins[_bin_index(len(sentence), _LENGTH_BINS)] += 1

        for ch in text:
            if ch in _PUNCT:
                punct_counts[ch] += 1
        for filler in _FILLER_CANDIDATES:
            if filler in text:
                filler_counts[filler] += text.count(filler)

        if text and text[-1] not in _SENTENCE_END and text[-1] not in _PUNCT:
            ends_without_punct += 1

    n = len(texts)
    bin_keys = list(range(len(_LENGTH_BINS) - 1))
    return {
        "sample_size": n,
        "sentence_length": _normalize(length_bins, bin_keys),
        "message_length": _normalize(msg_len_bins, bin_keys),
        "punctuation": _normalize(punct_counts, list(_PUNCT)),
        "filler": _normalize(filler_counts, _FILLER_CANDIDATES),
        # 标量特征：比率类，直接比差值
        "emoji_rate": emoji_chars / max(1, total_chars),
        "avg_sentences_per_message": sum(sentences_per_message) / n,
        "no_end_punct_rate": ends_without_punct / n,
    }


def _js_divergence(p: list[float], q: list[float]) -> float:
    """Jensen-Shannon 散度，取值 [0, 1]。

    用 JS 而非 KL：JS 对称且有界，某一箱在一侧为 0 时不会炸到无穷——
    生成文本完全不用某个标点是很常见的情况。
    """
    if len(p) != len(q):
        raise ValueError("分布维度不一致")

    def _kl(a: list[float], b: list[float]) -> float:
        total = 0.0
        for ai, bi in zip(a, b):
            if ai > 0 and bi > 0:
                total += ai * math.log2(ai / bi)
        return total

    m = [(pi + qi) / 2 for pi, qi in zip(p, q)]
    return max(0.0, min(1.0, 0.5 * _kl(p, m) + 0.5 * _kl(q, m)))


# 各维度权重。句长与消息长度最能体现「说话方式」，权重给高。
_WEIGHTS = {
    "sentence_length": 0.30,
    "message_length": 0.25,
    "punctuation": 0.15,
    "filler": 0.15,
    "emoji_rate": 0.05,
    "avg_sentences_per_message": 0.05,
    "no_end_punct_rate": 0.05,
}


def distance(a: dict, b: dict) -> float:
    """两份指纹的加权距离，0 = 完全一致，1 = 完全不同。"""
    if not a.get("sample_size") or not b.get("sample_size"):
        raise ValueError("指纹样本为空，无法比较")

    total = 0.0
    for key, weight in _WEIGHTS.items():
        left, right = a[key], b[key]
        if isinstance(left, list):
            total += weight * _js_divergence(left, right)
        else:
            # 标量：avg_sentences 可能大于 1，做个软归一
            scale = max(1.0, abs(left), abs(right))
            total += weight * min(1.0, abs(left - right) / scale)
    return round(total, 6)


def drift(early_texts: list[str], late_texts: list[str]) -> float:
    """人格漂移：后期表达相对前期的偏移。

    长对话里语气逐渐滑向「通用 AI 腔」是这类产品的典型失败模式，
    但没人量过它。
    """
    return distance(fingerprint(early_texts), fingerprint(late_texts))


def describe(fp: dict) -> dict:
    """指纹的人类可读摘要，用于报告与排查。"""
    if not fp.get("sample_size"):
        return {"sample_size": 0}
    bins = ["0-2", "3-5", "6-9", "10-15", "16-24", "25-39", "40-69", "70+"]
    top_punct = sorted(
        zip(_PUNCT, fp["punctuation"]), key=lambda x: x[1], reverse=True
    )[:5]
    top_filler = sorted(
        zip(_FILLER_CANDIDATES, fp["filler"]), key=lambda x: x[1], reverse=True
    )[:5]
    return {
        "sample_size": fp["sample_size"],
        "dominant_sentence_length": bins[
            fp["sentence_length"].index(max(fp["sentence_length"]))
        ],
        "top_punctuation": [p for p, v in top_punct if v > 0],
        "top_filler": [f for f, v in top_filler if v > 0],
        "emoji_rate": round(fp["emoji_rate"], 4),
        "avg_sentences_per_message": round(fp["avg_sentences_per_message"], 2),
        "no_end_punct_rate": round(fp["no_end_punct_rate"], 3),
    }


def target_texts_from_corpus(
    messages: list[dict], target_only: bool = True
) -> list[str]:
    """从语料归档里取出「ta」说的话。

    默认只取 target：混入用户自己的发言会把指纹拉平。
    """
    out = []
    for message in messages:
        if target_only and not message.get("is_target"):
            continue
        content = message.get("content")
        if content:
            out.append(str(content))
    return out


def fingerprint_for_mirror(slug: str, owner: Optional[int] = None) -> dict:
    """镜像的真人语料指纹，作为比对基线。"""
    from core.corpus_store import load_messages

    return fingerprint(target_texts_from_corpus(load_messages(slug, owner)))
