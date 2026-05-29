"""情感分析模块：基于关键词匹配的聊天记录情感倾向分析。"""

import re
from collections import defaultdict
from datetime import datetime
from typing import Optional

# ── 情感词典 ──

POSITIVE_WORDS = [
    # 开心/喜爱
    "开心", "高兴", "快乐", "幸福", "好开心", "太棒了", "哈哈哈", "嘻嘻",
    "爱", "喜欢", "想你", "想见你", "有你真好", "你真好", "好爱",
    "甜蜜", "温暖", "感动", "期待", "兴奋", "完美", "太好了", "好耶",
    "宝贝", "亲爱的", "么么", "抱抱", "亲亲", "mua",
    # 认可/感谢
    "谢谢", "感谢", "辛苦了", "厉害", "牛", "nb", "yyds", "绝了",
    "可以的", "不错", "棒", "优秀", "赞",
    # 撒娇（正面）
    "嘿嘿", "嘻嘻", "嘻嘻嘻", "呀", "哇", "耶",
]

NEGATIVE_WORDS = [
    # 生气/不满
    "生气", "讨厌", "烦", "烦死了", "滚", "走开", "别烦我",
    "你什么意思", "说了多少次", "说了多少遍", "听不懂吗",
    "随便", "无所谓", "你说了算", "行吧", "哦",
    # 难过/失望
    "难过", "伤心", "心碎", "失望", "心痛", "哭", "呜呜", "委屈",
    "你不理我", "你都不理我", "你不在乎", "你不在乎我",
    "分手", "再见", "别联系了",
    # 冷淡
    "嗯", "哦哦", "好的", "知道了", "随便你",
    # 吃醋
    "你跟谁", "又是谁", "你是不是", "那个女的", "那个男的",
]

NEUTRAL_WORDS = [
    "哦", "嗯嗯", "好的好的", "收到", "了解", "ok", "OK",
    "是的", "对", "对吧", "对哦",
]


def _count_hits(text: str, word_list: list[str]) -> int:
    """统计文本中命中的情感词数量。"""
    count = 0
    for word in word_list:
        count += text.count(word)
    return count


def analyze_sentiment(text: str) -> dict:
    """分析单条消息的情感倾向。

    Returns:
        {"label": "positive"|"negative"|"neutral", "score": float, "positive": int, "negative": int}
    """
    pos = _count_hits(text, POSITIVE_WORDS)
    neg = _count_hits(text, NEGATIVE_WORDS)
    total = pos + neg

    if total == 0:
        return {"label": "neutral", "score": 0.0, "positive": pos, "negative": neg}

    # 归一化得分：-1（极负面）到 +1（极正面）
    score = (pos - neg) / total

    if score > 0.2:
        label = "positive"
    elif score < -0.2:
        label = "negative"
    else:
        label = "neutral"

    return {"label": label, "score": round(score, 2), "positive": pos, "negative": neg}


def analyze_history(history: list[dict]) -> dict:
    """分析对话历史的整体情感倾向。

    Args:
        history: [{"role": "user"|"assistant", "content": "..."}]

    Returns:
        {
            "overall": {"label": "...", "score": float},
            "user_sentiment": {"label": "...", "score": float},
            "assistant_sentiment": {"label": "...", "score": float},
            "message_count": int,
            "positive_count": int,
            "negative_count": int,
            "neutral_count": int,
        }
    """
    all_scores = []
    user_scores = []
    assistant_scores = []
    labels = {"positive": 0, "negative": 0, "neutral": 0}

    for msg in history:
        content = msg.get("content", "")
        if not content.strip():
            continue
        result = analyze_sentiment(content)
        all_scores.append(result["score"])
        labels[result["label"]] += 1

        if msg.get("role") == "user":
            user_scores.append(result["score"])
        else:
            assistant_scores.append(result["score"])

    def _avg(scores):
        if not scores:
            return 0.0
        return round(sum(scores) / len(scores), 2)

    def _label_from_score(s):
        if s > 0.2:
            return "positive"
        elif s < -0.2:
            return "negative"
        return "neutral"

    user_avg = _avg(user_scores)
    assistant_avg = _avg(assistant_scores)
    overall_avg = _avg(all_scores)

    return {
        "overall": {"label": _label_from_score(overall_avg), "score": overall_avg},
        "user_sentiment": {"label": _label_from_score(user_avg), "score": user_avg},
        "assistant_sentiment": {"label": _label_from_score(assistant_avg), "score": assistant_avg},
        "message_count": len(all_scores),
        "positive_count": labels["positive"],
        "negative_count": labels["negative"],
        "neutral_count": labels["neutral"],
    }


def generate_emotion_curve(history: list[dict], bucket_size: int = 10) -> list[dict]:
    """按消息分段生成情感曲线数据。

    Args:
        history: 完整对话历史
        bucket_size: 每段包含的消息数（默认 10 条为一个时间段）

    Returns:
        [{"bucket": 0, "start": 0, "end": 9, "score": 0.3, "label": "positive", "count": 10}]
    """
    messages = [m for m in history if m.get("content", "").strip()]
    if not messages:
        return []

    buckets = []
    for i in range(0, len(messages), bucket_size):
        chunk = messages[i:i + bucket_size]
        scores = []
        for msg in chunk:
            result = analyze_sentiment(msg["content"])
            scores.append(result["score"])

        avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
        if avg_score > 0.2:
            label = "positive"
        elif avg_score < -0.2:
            label = "negative"
        else:
            label = "neutral"

        buckets.append({
            "bucket": len(buckets),
            "start": i,
            "end": min(i + bucket_size - 1, len(messages) - 1),
            "score": avg_score,
            "label": label,
            "count": len(chunk),
        })

    return buckets


def get代表性原话(history: list[dict], keyword: str, max_results: int = 3) -> list[str]:
    """从聊天记录中检索包含关键词的代表性原话。"""
    results = []
    seen = set()
    for msg in history:
        content = msg.get("content", "").strip()
        if keyword in content and content not in seen and len(content) > 2:
            seen.add(content)
            results.append(content)
            if len(results) >= max_results:
                break
    return results
