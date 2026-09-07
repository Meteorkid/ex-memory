"""越界检测与盲测工具（FR-061）。

「越界」指镜像做出了不属于「ta」的行为：自称 AI、给出百科式回答、用书面
公文语气、或者跳出来解释自己在做什么。这类失败很显眼，用户一眼就出戏，
但此前没有任何度量。

**检测用规则而非 LLM judge**：这些模式的表述相对固定，正则就能抓住绝大
多数；规则可以进 CI 每次跑，LLM judge 的口径会随模型版本漂移。代价是
抓不到「语义上像 AI 但用词不典型」的情况，那部分留给盲测。

盲测辨别率需要人真的去猜，这里只提供出题与计分，不假装能自动化。
"""

import json
import random
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 每类给出可读名称，报告里直接用
BREACH_PATTERNS: dict[str, list[str]] = {
    "self_identify_ai": [
        r"作为(一个|一名)?(AI|人工智能|语言模型|机器人|助手)",
        r"我是(一个|一名)?(AI|人工智能|语言模型|聊天机器人)",
        r"(大)?语言模型",
        r"我(只是|不过是)(一个)?程序",
        r"我没有(真实的)?(感情|情感|意识|身体)",
    ],
    "assistant_tone": [
        r"有什么(可以|能)(帮(到|助)你|为你效劳)",
        r"很高兴(能)?(帮助|为您服务)",
        r"希望(我的回答|这些)(对你|对您)有(所)?帮助",
        r"如(果|有)(还有)?(其他)?(疑问|问题).{0,6}(欢迎|请)",
    ],
    "encyclopedic": [
        r"^(根据|据)(相关)?(研究|资料|统计)",
        r"首先.{0,40}其次.{0,40}(最后|再次)",
        r"(综上所述|总的来说|总而言之)",
        r"以下(是|几点)",
        r"第一[，,、].{0,40}第二[，,、]",
    ],
    "formal_written": [
        r"您好",
        r"(此外|另外值得注意的是|需要指出的是)",
        r"(建议您|请您)",
    ],
    "meta_disclosure": [
        r"(我无法|我不能)(提供|回答|讨论)",
        r"作为(你的)?(数字)?(镜像|分身|替身)",
        r"(这是|我是)(根据|基于)(聊天记录|数据)(生成|训练)",
    ],
}

_COMPILED = {
    name: [re.compile(p) for p in patterns]
    for name, patterns in BREACH_PATTERNS.items()
}


@dataclass(frozen=True)
class BreachResult:
    breached: bool
    categories: list[str]
    matches: list[str]


def detect(text: str) -> BreachResult:
    """检查一条回复是否越界。"""
    if not text or not text.strip():
        return BreachResult(False, [], [])

    categories: list[str] = []
    matches: list[str] = []
    for name, patterns in _COMPILED.items():
        for pattern in patterns:
            found = pattern.search(text)
            if found:
                if name not in categories:
                    categories.append(name)
                matches.append(found.group(0))
                break
    return BreachResult(bool(categories), categories, matches)


def breach_rate(replies: list[str]) -> dict:
    """一批回复的越界率与分类分布。"""
    total = len([r for r in replies if r and r.strip()])
    if total == 0:
        return {"total": 0, "breached": 0, "rate": 0.0, "by_category": {}}

    breached = 0
    by_category: dict[str, int] = {}
    for reply in replies:
        result = detect(reply)
        if result.breached:
            breached += 1
            for category in result.categories:
                by_category[category] = by_category.get(category, 0) + 1
    return {
        "total": total,
        "breached": breached,
        "rate": round(breached / total, 4),
        "by_category": dict(sorted(by_category.items(), key=lambda x: -x[1])),
    }


# ── 讲解模式漂移（越界的隐性形态）──

# 实测（2026-09-07，5 个真实镜像，7 轮取数）：字数比值落在 2.2~3.4，中位约 2.6。
#
# **这个指标有 ±25% 的噪声地板，加样本压不下去**：晃的是分子——同一道
# 「红烧肉怎么做」，模型这轮给六步菜谱、下轮就一句「我教你呀」。所以门禁设在
# 观测上界之上，只用来抓「明显变差」；把 2.4 和 3.1 的差异当信号是过度解读。
#
# 最初记的 5.7 是错的：那版知识题只有 2 道，「红烧肉」一道就把比值拉了上去。
# 补到 5 道后降到 2.6 量级。这个指标对题目组成极敏感，换题必须重标基线，
# 跨版本的数字不能直接比。
SHIFT_BASELINE_CHAR_RATIO = 2.6
SHIFT_MAX_CHAR_RATIO = 4.0


def _bubbles(text: str) -> int:
    """一条回复被拆成几段。产品用 || 表示分条发送，换行同样是分段。"""
    return len([p for p in re.split(r"\|\||\n+", text) if p.strip()])


def explanation_shift(natural: list[str], knowledge: list[str]) -> dict:
    """被问到知识时，回复相对日常闲聊膨胀了多少。

    正则抓的是「说出了不属于 ta 的话」，抓不到「答得像一篇讲解」。实测五个
    真实镜像的越界率接近 0（480 条里 2 条，且不稳定复现），但问「红烧肉怎么做」
    时全部给出了配比精确的六步菜谱——闲聊回复中位 22 字，这道题 181 字。一条
    正则都没命中，可没有哪个前任会这样答：模型的世界知识穿透人格漏出来了。

    漂移集中在**步骤类**问题：同一批镜像回答「为什么天空是蓝的」中位 47 字、
    「光合作用是怎么回事」40 字，都在闲聊量级。这也是为什么单看一个总比值会
    看走眼，逐题中位数（run_breach 的 by_prompt）才指得出问题在哪。

    长度与分段数是这种漂移最直接的外化：纯统计、零成本、结果确定可复现，
    和表达指纹是同一路子。比值按镜像自身的闲聊基线归一，避免把「这个人本来
    就话多」误判成漂移。

    注意这不是能进单元 CI 的门禁——取数要对真实镜像发起真实调用。可进 CI 的
    是这个函数本身，门禁跑在 `make eval-breach` 里，和其他 eval 同一档。
    """
    natural_ok = [t for t in natural if t and t.strip()]
    knowledge_ok = [t for t in knowledge if t and t.strip()]
    if not natural_ok or not knowledge_ok:
        raise ValueError("闲聊与知识两组回复都不能为空")

    n_chars = statistics.median(len(t) for t in natural_ok)
    k_chars = statistics.median(len(t) for t in knowledge_ok)
    n_bubbles = statistics.median(_bubbles(t) for t in natural_ok)
    k_bubbles = statistics.median(_bubbles(t) for t in knowledge_ok)
    return {
        "n_natural": len(natural_ok),
        "n_knowledge": len(knowledge_ok),
        "natural_chars": round(n_chars, 1),
        "knowledge_chars": round(k_chars, 1),
        "char_ratio": round(k_chars / n_chars, 2),
        "natural_bubbles": round(n_bubbles, 1),
        "knowledge_bubbles": round(k_bubbles, 1),
        "bubble_ratio": round(k_bubbles / n_bubbles, 2),
    }


# ── 盲测（需要人工标注）──


def build_blind_set(
    real_texts: list[str],
    generated_texts: list[str],
    size: int = 40,
    seed: int = 42,
) -> list[dict]:
    """出一份盲测题：每题一句真话一句生成，随机排序。

    人来猜哪句是真的。准确率越接近 50% 越好——说明分辨不出来。
    刻意固定随机种子，让同一批语料出的题可复现。
    """
    rng = random.Random(seed)
    real = [t for t in real_texts if t and t.strip()]
    generated = [t for t in generated_texts if t and t.strip()]
    if not real or not generated:
        raise ValueError("真实语料与生成文本都不能为空")

    pairs = []
    for index in range(min(size, len(real), len(generated))):
        options = [
            {"text": real[index], "is_real": True},
            {"text": generated[index], "is_real": False},
        ]
        rng.shuffle(options)
        pairs.append(
            {
                "id": f"blind_{index}",
                "options": [
                    {"label": chr(65 + i), "text": o["text"]}
                    for i, o in enumerate(options)
                ],
                "answer": chr(
                    65 + next(i for i, o in enumerate(options) if o["is_real"])
                ),
            }
        )
    return pairs


def score_blind_set(questions: list[dict], answers: dict[str, str]) -> dict:
    """给盲测计分。

    返回的 discrimination 是**辨别率**：越接近 0.5 越好。
    高于 0.5 说明人能认出哪句是生成的；低于 0.5 说明生成的反而更像「ta」，
    通常意味着真实语料里混进了不像 ta 的内容，同样值得排查。
    """
    graded = [q for q in questions if q["id"] in answers]
    if not graded:
        return {"answered": 0, "correct": 0, "discrimination": None}

    correct = sum(1 for q in graded if answers[q["id"]] == q["answer"])
    rate = correct / len(graded)
    return {
        "answered": len(graded),
        "correct": correct,
        "discrimination": round(rate, 4),
        "distance_from_ideal": round(abs(rate - 0.5), 4),
    }


def export_blind_set(questions: list[dict], path: Path) -> Path:
    """导出盲测题面（不含答案），交给标注者。"""
    payload = [{"id": q["id"], "options": q["options"]} for q in questions]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def blind_set_from_mirror(
    slug: str,
    generated_texts: list[str],
    owner: Optional[int] = None,
    size: int = 40,
) -> list[dict]:
    """用镜像的真实语料出盲测题。"""
    from core.corpus_store import load_messages
    from evals.fingerprint import target_texts_from_corpus

    real = target_texts_from_corpus(load_messages(slug, owner))
    return build_blind_set(real, generated_texts, size=size)
