"""个性化模块：学习用户习惯，提供个性化体验。"""

import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ex-memory")


def analyze_user_style(messages: list[dict]) -> dict:
    """分析用户的对话风格。

    Args:
        messages: [{"role": "user"|"assistant", "content": "...", "created_at": "..."}]

    Returns:
        {
            "common_words": [...],
            "avg_message_length": float,
            "reply_speed": "fast"|"normal"|"slow",
            "preferred_topics": [...],
            "emotional_tendency": "positive"|"negative"|"neutral",
        }
    """
    user_messages = [m for m in messages if m.get("role") == "user"]

    if not user_messages:
        return {
            "common_words": [],
            "avg_message_length": 0,
            "reply_speed": "normal",
            "preferred_topics": [],
            "emotional_tendency": "neutral",
        }

    # 分析常用词
    all_words = []
    total_length = 0
    for msg in user_messages:
        content = msg.get("content", "")
        total_length += len(content)
        # 简单分词（按空格和标点）
        words = content.replace("，", " ").replace("。", " ").replace("！", " ").replace("？", " ").split()
        all_words.extend(words)

    word_counter = Counter(all_words)
    common_words = [word for word, count in word_counter.most_common(10) if len(word) > 1]

    # 分析消息长度
    avg_length = total_length / len(user_messages) if user_messages else 0

    # 分析回复速度（基于时间间隔）
    reply_speed = "normal"
    if avg_length < 20:
        reply_speed = "fast"
    elif avg_length > 100:
        reply_speed = "slow"

    # 分析情感倾向
    positive_words = ["开心", "高兴", "快乐", "幸福", "哈哈", "太好了", "爱", "喜欢"]
    negative_words = ["难过", "伤心", "生气", "讨厌", "烦", "哭", "失望"]

    positive_count = sum(1 for msg in user_messages for word in positive_words if word in msg.get("content", ""))
    negative_count = sum(1 for msg in user_messages for word in negative_words if word in msg.get("content", ""))

    if positive_count > negative_count * 1.5:
        emotional_tendency = "positive"
    elif negative_count > positive_count * 1.5:
        emotional_tendency = "negative"
    else:
        emotional_tendency = "neutral"

    return {
        "common_words": common_words[:10],
        "avg_message_length": round(avg_length, 1),
        "reply_speed": reply_speed,
        "preferred_topics": [],  # 需要更复杂的NLP分析
        "emotional_tendency": emotional_tendency,
    }


def calculate_relationship_temperature(slug: str, messages: list[dict]) -> dict:
    """计算关系温度（亲密度）。

    Returns:
        {
            "temperature": 0-100,
            "level": "hot"|"warm"|"cool"|"cold",
            "factors": {...},
        }
    """
    if not messages:
        return {"temperature": 50, "level": "warm", "factors": {}}

    user_messages = [m for m in messages if m.get("role") == "user"]
    assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    # 因素1: 对话频率（消息数量）
    message_count = len(messages)
    frequency_score = min(message_count / 100, 1.0) * 30  # 最高30分

    # 因素2: 情感强度
    love_words = ["爱", "喜欢", "想你", "想见你", "宝贝", "亲爱的"]
    love_count = sum(1 for msg in messages for word in love_words if word in msg.get("content", ""))
    emotion_score = min(love_count / 20, 1.0) * 25  # 最高25分

    # 因素3: 互动平衡（用户和助手消息比例）
    if user_messages and assistant_messages:
        balance = min(len(user_messages), len(assistant_messages)) / max(len(user_messages), len(assistant_messages))
        balance_score = balance * 20  # 最高20分
    else:
        balance_score = 0

    # 因素4: 对话深度（平均消息长度）
    avg_length = sum(len(m.get("content", "")) for m in messages) / len(messages)
    depth_score = min(avg_length / 100, 1.0) * 15  # 最高15分

    # 因素5: 时间跨度
    if messages:
        try:
            first_time = datetime.fromisoformat(messages[0].get("created_at", ""))
            last_time = datetime.fromisoformat(messages[-1].get("created_at", ""))
            days = (last_time - first_time).days
            time_score = min(days / 30, 1.0) * 10  # 最高10分
        except:
            time_score = 5
    else:
        time_score = 5

    # 计算总分
    temperature = round(frequency_score + emotion_score + balance_score + depth_score + time_score)
    temperature = max(0, min(100, temperature))

    # 确定等级
    if temperature >= 80:
        level = "hot"
    elif temperature >= 60:
        level = "warm"
    elif temperature >= 40:
        level = "cool"
    else:
        level = "cold"

    return {
        "temperature": temperature,
        "level": level,
        "factors": {
            "frequency": round(frequency_score, 1),
            "emotion": round(emotion_score, 1),
            "balance": round(balance_score, 1),
            "depth": round(depth_score, 1),
            "time": round(time_score, 1),
        },
    }


def save_user_profile(slug: str, profile: dict):
    """保存用户画像。"""
    from config import get_ex_dir
    ex_dir = get_ex_dir(slug)
    profile_file = ex_dir / "user_profile.json"

    with open(profile_file, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)

    logger.info("用户画像已保存: %s", slug)


def load_user_profile(slug: str) -> dict:
    """加载用户画像。"""
    from config import get_ex_dir
    ex_dir = get_ex_dir(slug)
    profile_file = ex_dir / "user_profile.json"

    if not profile_file.exists():
        return {}

    with open(profile_file, "r", encoding="utf-8") as f:
        return json.load(f)
