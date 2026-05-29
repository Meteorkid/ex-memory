"""记忆重要度评分：基于规则的消息重要性评估（不依赖外部 API）。"""

import re
from typing import Optional


# ── 情感词（强度分级）──

HIGH_EMOTION_WORDS = [
    # 强烈正面
    "爱你", "好爱", "最爱", "想你", "好想你", "永远", "一辈子", "承诺",
    "感动", "幸福", "心痛", "心碎", "崩溃",
    # 强烈负面
    "分手", "不爱了", "别联系", "死心", "绝望", "恨", "背叛", "欺骗",
    # 强烈感叹
    "天哪", "我靠", "卧槽", "救命", "啊啊啊",
]

MEDIUM_EMOTION_WORDS = [
    # 中等正面
    "开心", "高兴", "快乐", "喜欢", "爱", "甜蜜", "温暖", "期待",
    "好开心", "太棒了", "哈哈哈", "嘻嘻", "宝贝", "亲爱的", "么么",
    "谢谢", "辛苦了", "有你真好", "你真好",
    # 中等负面
    "生气", "讨厌", "烦", "难过", "伤心", "失望", "委屈", "哭",
    "不理我", "不在乎", "吃醋", "你跟谁",
]

LOW_EMOTION_WORDS = [
    "嗯", "哦", "好的", "知道了", "收到", "ok", "OK", "是的", "对",
]


# ── 具体细节模式 ──

# 时间表达
TIME_PATTERNS = [
    r'\d{4}年', r'\d{1,2}月', r'\d{1,2}[日号]',
    r'昨天', r'今天', r'明天', r'后天', r'前天',
    r'上周', r'下周', r'上个月', r'下个月',
    r'\d{1,2}:\d{2}',  # 时间点
    r'凌晨', r'早上', r'上午', r'中午', r'下午', r'傍晚', r'晚上', r'深夜',
    r'生日', r'纪念日', r'周年',
]

# 地点表达
LOCATION_PATTERNS = [
    r'[东南西北]京', r'上海', r'广州', r'深圳', r'杭州', r'成都', r'武汉',
    r'大学', r'学校', r'公司', r'医院', r'机场', r'车站',
    r'公园', r'商场', r'餐厅', r'咖啡', r'酒吧', r'KTV',
    r'家', r'家里', r'宿舍', r'酒店', r'宾馆',
]

# 事件/活动
EVENT_PATTERNS = [
    r'旅行', r'旅游', r'出差', r'面试', r'考试', r'毕业',
    r'结婚', r'搬家', r'辞职', r'入职', r'升职',
    r'生病', r'住院', r'手术', r'受伤',
    r'吵架', r'和好', r'表白', r'求婚',
]


def _count_pattern_hits(text: str, patterns: list[str]) -> int:
    """统计文本中匹配的模式数量。"""
    count = 0
    for pattern in patterns:
        matches = re.findall(pattern, text)
        count += len(matches)
    return count


def _count_emotion_hits(text: str, word_list: list[str]) -> int:
    """统计文本中命中的情感词数量。"""
    count = 0
    for word in word_list:
        count += text.count(word)
    return count


def calculate_importance(content: str) -> float:
    """计算消息的重要性分数（0.0 - 1.0）。

    评分因素：
    1. 消息长度（太短的日常寒暄低分，有内容的高分）
    2. 情感词密度（情感越强烈越重要）
    3. 具体细节（包含时间、地点、事件的更具体更有记忆价值）
    4. 特殊标记（问号、感叹号、省略号等表达情绪的标点）

    Args:
        content: 消息文本

    Returns:
        0.0-1.0 的重要性分数
    """
    if not content or not content.strip():
        return 0.0

    text = content.strip()
    length = len(text)

    # ── 因子 1：消息长度分（0-0.25）──
    # 极短消息（<5字）几乎无信息量
    if length < 5:
        length_score = 0.05
    elif length < 15:
        length_score = 0.1
    elif length < 50:
        length_score = 0.15
    elif length < 200:
        length_score = 0.2
    else:
        length_score = 0.25

    # ── 因子 2：情感强度分（0-0.35）──
    high_emotion = _count_emotion_hits(text, HIGH_EMOTION_WORDS)
    medium_emotion = _count_emotion_hits(text, MEDIUM_EMOTION_WORDS)
    low_emotion = _count_emotion_hits(text, LOW_EMOTION_WORDS)

    # 归一化情感得分
    raw_emotion = high_emotion * 3 + medium_emotion * 2 + low_emotion * 0.5
    # 用 sigmoid-like 函数映射到 0-0.35
    emotion_score = min(0.35, raw_emotion * 0.08)

    # ── 因子 3：具体细节分（0-0.25）──
    time_hits = _count_pattern_hits(text, TIME_PATTERNS)
    location_hits = _count_pattern_hits(text, LOCATION_PATTERNS)
    event_hits = _count_pattern_hits(text, EVENT_PATTERNS)

    detail_raw = time_hits * 1.5 + location_hits * 1.0 + event_hits * 2.0
    detail_score = min(0.25, detail_raw * 0.08)

    # ── 因子 4：标点情绪分（0-0.15）──
    exclamation = text.count("！") + text.count("!")
    question = text.count("？") + text.count("?")
    ellipsis = text.count("…") + text.count("...")
    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001F900-\U0001F9FF"
        "\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF]"
    )
    emoji_count = len(emoji_pattern.findall(text))

    punctuation_raw = exclamation * 1.0 + question * 0.5 + ellipsis * 0.8 + emoji_count * 0.5
    punctuation_score = min(0.15, punctuation_raw * 0.05)

    # ── 总分 ──
    total = length_score + emotion_score + detail_score + punctuation_score
    return round(min(1.0, max(0.0, total)), 3)


def should_keep_memory(importance: float, age_days: int) -> bool:
    """判断记忆是否应该保留（基于重要度和时间衰减）。

    衰减规则：
    - 重要度 >= 0.8：永久保留
    - 重要度 >= 0.5：保留 90 天
    - 重要度 >= 0.3：保留 30 天
    - 重要度 < 0.3：保留 7 天

    Args:
        importance: 重要性分数 (0.0-1.0)
        age_days: 记忆年龄（天数）

    Returns:
        True 表示应保留，False 表示可丢弃
    """
    if importance >= 0.8:
        return True  # 高重要度：永久保留
    elif importance >= 0.5:
        return age_days <= 90  # 中高重要度：90 天
    elif importance >= 0.3:
        return age_days <= 30  # 中等重要度：30 天
    else:
        return age_days <= 7  # 低重要度：7 天


def get_decay_info(importance: float) -> dict:
    """获取记忆衰减信息（用于展示）。

    Returns:
        {"level": "permanent"|"long"|"medium"|"short", "ttl_days": int|None, "description": str}
    """
    if importance >= 0.8:
        return {"level": "permanent", "ttl_days": None, "description": "永久记忆"}
    elif importance >= 0.5:
        return {"level": "long", "ttl_days": 90, "description": "长期记忆（90天）"}
    elif importance >= 0.3:
        return {"level": "medium", "ttl_days": 30, "description": "中期记忆（30天）"}
    else:
        return {"level": "short", "ttl_days": 7, "description": "短期记忆（7天）"}
