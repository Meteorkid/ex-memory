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
