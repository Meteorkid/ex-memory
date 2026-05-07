"""对话纠正检测与处理：识别用户纠正，更新 memory/persona/SKILL。"""

import json
import re
from datetime import datetime
from pathlib import Path
from openai import OpenAI
from config import get_llm_config, get_ex_dir

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 触发纠正的关键词模式
CORRECTION_TRIGGERS = [
    r"不对",
    r"不是这样",
    r"ta不会这样[说讲]",
    r"ta应该是",
    r"ta其实是",
    r"这不像ta",
    r"感觉不对",
    r"太温柔了",
    r"太冷漠了",
    r"太正式了",
    r"ta没这么",
    r"ta不用这个",
    r"不像ta",
    r"不是ta的风格",
]


def detect_correction(user_msg: str) -> bool:
    """检测用户消息是否包含纠正意图。"""
    for pattern in CORRECTION_TRIGGERS:
        if re.search(pattern, user_msg):
            return True
    return False


def handle_correction(
    slug: str,
    user_msg: str,
    last_reply: str,
    history: list[dict],
) -> str:
    """处理用户纠正，生成修正内容并写入 corrections.md。

    Args:
        slug: 前任代号
        user_msg: 用户的纠正消息
        last_reply: 被纠正的上一条回复
        history: 对话历史

    Returns:
        处理结果的确认消息
    """
    cfg = get_llm_config()
    if not cfg["api_key"]:
        return "（未配置 LLM API Key，无法自动处理纠正。请手动编辑 corrections.md。）"

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    ex_dir = get_ex_dir(slug)

    # 读取 correction_handler prompt
    prompt_template = (PROMPTS_DIR / "correction_handler.md").read_text(encoding="utf-8")

    # 构建上下文
    context = f"""## 纠正上下文

用户消息：{user_msg}

被纠正的回复：{last_reply}

最近对话：
{_format_history(history[-6:])}
"""

    # 调用 LLM 生成纠正内容
    response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": prompt_template},
            {"role": "user", "content": context},
        ],
        temperature=0.3,
    )
    correction_content = response.choices[0].message.content

    # 写入 corrections.md
    corrections_path = ex_dir / "corrections.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    existing = ""
    if corrections_path.exists():
        existing = corrections_path.read_text(encoding="utf-8")
        # 计算已有纠正数量
        count = len(re.findall(r"### Correction #", existing))
    else:
        existing = "# 纠正记录\n\n"
        count = 0

    new_correction = f"""
### Correction #{count + 1} — {timestamp}

{correction_content}

---
"""
    corrections_path.write_text(existing + new_correction, encoding="utf-8")

    # 同时追加到 memory.md 的 Correction 记录节
    _append_to_memory(slug, count + 1, timestamp, user_msg)

    # 重新生成 SKILL.md
    from pipeline.skill_combiner import write_skill
    write_skill(slug)

    return f"已记录纠正 #{count + 1}，下条回复会体现。"


def _append_to_memory(slug: str, correction_num: int, timestamp: str, user_msg: str):
    """将纠正摘要追加到 memory.md 的 Correction 记录节。"""
    ex_dir = get_ex_dir(slug)
    memory_path = ex_dir / "memory.md"

    if not memory_path.exists():
        return

    content = memory_path.read_text(encoding="utf-8")

    # 如果没有 Correction 记录节，追加
    if "## Correction 记录" not in content:
        content += "\n\n## Correction 记录\n"

    correction_entry = f"""
### Correction #{correction_num} — {timestamp}
- 用户原话："{user_msg}"
- 详见 corrections.md
"""
    content += correction_entry
    memory_path.write_text(content, encoding="utf-8")


def _format_history(messages: list[dict]) -> str:
    """格式化对话历史为可读文本。"""
    lines = []
    for msg in messages:
        role = "我" if msg.get("role") == "user" else "ta"
        lines.append(f"{role}: {msg.get('content', '')[:200]}")
    return "\n".join(lines)
