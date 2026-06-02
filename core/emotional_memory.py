"""情感记忆模块：提取和存储重要情感记忆点。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ex-memory")

# 情感关键词
EMOTIONAL_KEYWORDS = {
    "love": ["爱", "喜欢", "想你", "想见你", "在一起", "在一起", "宝贝", "亲爱的", "老公", "老婆"],
    "happy": ["开心", "高兴", "快乐", "幸福", "太棒了", "哈哈", "嘻嘻", "好开心"],
    "sad": ["难过", "伤心", "心碎", "失望", "哭", "呜呜", "委屈", "心痛"],
    "angry": ["生气", "讨厌", "烦", "滚", "走开", "别烦我", "你什么意思"],
    "miss": ["想你", "想念", "思念", "好久不见", "什么时候见面", "想见你"],
    "memory": ["记得", "还记得", "那次", "那次在", "那个", "第一次", "上次"],
    "promise": ["答应", "承诺", "说过", "保证", "一定", "下次"],
}

# 重要日期模式
DATE_PATTERNS = {
    "birthday": ["生日", "生日快乐", "生辰"],
    "anniversary": ["纪念日", "周年", "在一起", "相识", "相恋"],
    "holiday": ["情人节", "圣诞节", "新年", "春节", "中秋", "七夕"],
    "special": ["第一次", "最后一次", "那天", "那天在"],
}


def extract_emotional_memories(messages: list[dict]) -> dict:
    """从对话历史中提取情感记忆点。

    Args:
        messages: [{"role": "user"|"assistant", "content": "...", "created_at": "..."}]

    Returns:
        {
            "important_dates": [...],
            "shared_experiences": [...],
            "emotional_milestones": [...],
            "pet_names": [...],
        }
    """
    important_dates = []
    shared_experiences = []
    emotional_milestones = []
    pet_names = set()

    for msg in messages:
        content = msg.get("content", "").lower()
        role = msg.get("role", "")
        timestamp = msg.get("created_at", "")

        # 提取称呼
        if role == "assistant":
            for name in ["宝贝", "亲爱的", "老公", "老婆", "傻瓜", "笨蛋", "臭宝"]:
                if name in content:
                    pet_names.add(name)

        # 提取重要日期
        for date_type, keywords in DATE_PATTERNS.items():
            for keyword in keywords:
                if keyword in content:
                    important_dates.append({
                        "type": date_type,
                        "content": msg.get("content", "")[:100],
                        "timestamp": timestamp,
                        "keyword": keyword,
                    })
                    break

        # 提取共同经历
        for memory_keyword in EMOTIONAL_KEYWORDS["memory"]:
            if memory_keyword in content:
                shared_experiences.append({
                    "content": msg.get("content", "")[:150],
                    "timestamp": timestamp,
                    "type": "memory",
                })
                break

        # 提取情感里程碑
        if any(word in content for word in ["第一次说爱你", "第一次牵手", "第一次拥抱", "第一次接吻"]):
            emotional_milestones.append({
                "content": msg.get("content", "")[:100],
                "timestamp": timestamp,
                "type": "milestone",
            })

    return {
        "important_dates": important_dates[:20],  # 限制数量
        "shared_experiences": shared_experiences[:30],
        "emotional_milestones": emotional_milestones[:10],
        "pet_names": list(pet_names)[:5],
    }


def save_emotional_memories(slug: str, memories: dict):
    """保存情感记忆到文件。"""
    from config import get_ex_dir
    ex_dir = get_ex_dir(slug)
    memory_file = ex_dir / "emotional_memories.json"

    with open(memory_file, "w", encoding="utf-8") as f:
        json.dump(memories, f, ensure_ascii=False, indent=2)

    logger.info("情感记忆已保存: %s", slug)


def load_emotional_memories(slug: str) -> dict:
    """加载情感记忆。"""
    from config import get_ex_dir
    ex_dir = get_ex_dir(slug)
    memory_file = ex_dir / "emotional_memories.json"

    if not memory_file.exists():
        return {
            "important_dates": [],
            "shared_experiences": [],
            "emotional_milestones": [],
            "pet_names": [],
        }

    with open(memory_file, "r", encoding="utf-8") as f:
        return json.load(f)


def get_memory_context(slug: str, current_topic: str = "") -> str:
    """获取情感记忆上下文，用于增强对话。"""
    memories = load_emotional_memories(slug)
    context_parts = []

    # 添加重要日期
    if memories["important_dates"]:
        dates = memories["important_dates"][:3]  # 取最近3个
        date_str = ", ".join([d["content"][:30] for d in dates])
        context_parts.append(f"重要记忆: {date_str}")

    # 添加共同经历
    if memories["shared_experiences"]:
        exps = memories["shared_experiences"][:2]  # 取最近2个
        exp_str = "; ".join([e["content"][:50] for e in exps])
        context_parts.append(f"共同经历: {exp_str}")

    # 添加称呼
    if memories["pet_names"]:
        names = ", ".join(memories["pet_names"][:3])
        context_parts.append(f"常用称呼: {names}")

    return "\n".join(context_parts) if context_parts else ""
