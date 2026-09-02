"""表达风格：从真人语料推出 ta 的说话方式，并据此塑形回复（FR-062~064、067）。

三个「出戏点」在这里处理：

1. **单气泡 vs 连发短消息**。真实微信里人是连发多条短消息的——「在吗」
   「刚看到」「今天好累」。一整段完整的话反而最不像微信。
2. **永远秒回**。无论深夜还是工作日下午都同样速度，真人不会。
3. **有求必应**。真人会敷衍、会短回、会不打标点。

做法是**从语料统计出 ta 的真实习惯，作为显式指令注入 prompt**，而不是
调高 temperature 制造随机——随机性不等于人性。
"""

import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ex-memory")

SPLIT_MARKER = "||"


@dataclass(frozen=True)
class StyleProfile:
    """ta 的表达习惯，全部从真实语料统计而来。"""

    avg_message_chars: float
    messages_per_reply: float
    no_end_punct_rate: float
    emoji_rate: float
    top_fillers: list[str]
    sample_size: int

    @property
    def is_terse(self) -> bool:
        """短句型：平均消息长度短且常不打句末标点。"""
        return self.avg_message_chars < 15 and self.no_end_punct_rate > 0.5


def profile_from_corpus(
    slug: str, owner: Optional[int] = None
) -> Optional[StyleProfile]:
    """从语料归档推出风格画像。没有归档时返回 None（不猜）。"""
    from core.corpus_store import load_messages
    from evals.fingerprint import describe, fingerprint, target_texts_from_corpus

    texts = target_texts_from_corpus(load_messages(slug, owner))
    if len(texts) < 20:
        # 样本太少统计不出稳定风格，宁可不注入也不要注入错的
        return None

    fp = fingerprint(texts)
    summary = describe(fp)
    return StyleProfile(
        avg_message_chars=sum(len(t) for t in texts) / len(texts),
        messages_per_reply=fp["avg_sentences_per_message"],
        no_end_punct_rate=fp["no_end_punct_rate"],
        emoji_rate=fp["emoji_rate"],
        top_fillers=summary["top_filler"][:3],
        sample_size=fp["sample_size"],
    )


def style_instructions(profile: Optional[StyleProfile]) -> str:
    """把风格画像转成 prompt 指令。没有画像时给通用的微信化指令。"""
    lines = [
        "\n---\n## 表达方式",
        f"把一次回复拆成多条短消息，条与条之间用 {SPLIT_MARKER} 分隔——"
        "微信里人是连发几条短消息的，一整段完整的话反而最不像。",
    ]
    if not isinstance(profile, StyleProfile):
        lines.append("多数消息控制在 15 字以内，句末常常不打标点。")
        return "\n".join(lines) + "\n"

    lines.append(
        f"ta 的真实习惯（统计自 {profile.sample_size} 条原话）："
        f"平均每条约 {profile.avg_message_chars:.0f} 字。"
    )
    if profile.is_terse:
        lines.append("ta 说话很短，别写长句，别展开解释。")
    if profile.no_end_punct_rate > 0.5:
        lines.append("ta 句末**经常不打标点**，直接结束。")
    if profile.emoji_rate < 0.005:
        lines.append("ta 几乎不用 emoji，不要加。")
    elif profile.emoji_rate > 0.02:
        lines.append("ta 会用 emoji，但别堆。")
    if profile.top_fillers:
        lines.append(f"ta 常用的语气词：{'、'.join(profile.top_fillers)}。")
    lines.append(
        "不必有求必应：可以敷衍、可以只回两个字、可以岔开话题。"
        "适度的不完美比句句切题更像真人。"
    )
    return "\n".join(lines) + "\n"


def split_reply(text: str, profile: Optional[StyleProfile] = None) -> list[str]:
    """把回复拆成多条消息。

    优先用模型自己给的分隔符——它知道哪里断句自然。没有分隔符时按句末标点
    兜底，但不强行把长句切碎：切错位置比不切更出戏。
    """
    if not text or not text.strip():
        return []

    if SPLIT_MARKER in text:
        parts = [p.strip() for p in text.split(SPLIT_MARKER)]
        return [p for p in parts if p]

    # 兜底：只在句末标点处断，且仅当整体明显偏长
    # 显式判类型而不是判真值：调用方传进来的可能是别的对象，
    # 拿它的属性去做数值比较会在运行时炸
    target_len = profile.avg_message_chars if isinstance(profile, StyleProfile) else 15
    if len(text) <= max(20, target_len * 1.5):
        return [text.strip()]

    parts, buf = [], []
    for ch in text:
        buf.append(ch)
        if ch in "。！？!?\n" and len("".join(buf).strip()) >= 4:
            parts.append("".join(buf).strip())
            buf = []
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in parts if p] or [text.strip()]


def reply_delay_seconds(
    message_index: int,
    text: str,
    profile: Optional[StyleProfile] = None,
    now: Optional[datetime] = None,
) -> float:
    """这条消息该等多久再发出。

    真人的回复速度受两件事影响：手上在打字（长消息更慢），以及此刻在干嘛
    （深夜、上班时间会慢）。永远秒回是最出戏的一点。
    """
    now = now or datetime.now()
    # 打字时间：按中文每秒约 4 字估
    typing = min(4.0, len(text) / 4.0)
    # 第一条要多一点「看到消息」的时间，后续是连打
    base = 1.2 if message_index == 0 else 0.4
    delay = base + typing

    hour = now.hour
    if 0 <= hour < 7:
        delay *= 2.5  # 深夜：可能在睡
    elif 9 <= hour < 18 and now.weekday() < 5:
        delay *= 1.6  # 工作日白天：在忙
    if isinstance(profile, StyleProfile) and profile.is_terse:
        delay *= 0.8  # 话少的人打字也快

    # 抖动：完全一致的间隔本身就很机器
    delay *= 0.75 + random.random() * 0.5
    return round(min(delay, 12.0), 2)


def relative_time_hint(
    last_seen_iso: Optional[str], now: Optional[datetime] = None
) -> str:
    """距上次对话多久（FR-067）。

    绝对时间感知（现在几点、星期几）此前已有；缺的是**相对时间**——
    「好久没聊了」这种话需要知道间隔，而不是知道今天是周三。
    """
    if not last_seen_iso:
        return "这是你们的第一次对话。"
    try:
        last = datetime.fromisoformat(last_seen_iso)
    except (ValueError, TypeError):
        return ""

    now = now or datetime.now()
    if last.tzinfo is not None and now.tzinfo is None:
        last = last.replace(tzinfo=None)
    gap = (now - last).total_seconds()
    if gap < 0:
        return ""

    if gap < 600:
        return "你们刚刚还在聊。"
    if gap < 3600:
        return f"距上次说话过了约 {int(gap // 60)} 分钟。"
    if gap < 86400:
        return f"距上次说话过了约 {int(gap // 3600)} 小时。"
    days = int(gap // 86400)
    if days == 1:
        return "上次说话是昨天。"
    if days < 30:
        return f"距上次说话已经 {days} 天了。"
    if days < 365:
        return f"距上次说话已经 {days // 30} 个多月了。"
    return "距上次说话已经一年多了。"
