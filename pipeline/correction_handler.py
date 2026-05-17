"""对话纠正检测与处理：识别用户纠正，更新 memory/persona/SKILL。"""

import json
import re
from datetime import datetime
from pathlib import Path
from config import get_llm_config, get_llm_client, get_ex_dir
from core.file_utils import atomic_write

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# 触发纠正的关键词模式 —— 必须包含对 ta（前任）的明确指向，避免日常对话误触发
CORRECTION_TRIGGERS = [
    r"ta(不|没).{0,4}(会|可能|应该|是|这么)",
    r"(这|那)(不|没).{0,6}(像|是)ta",
    r"ta.{0,6}(不是|不像|不对|没说|不会)",
    r"不对.{0,4}ta.{0,4}(是|会说)",
    r"不是ta的(风格|性格|语气|习惯)",
    r"ta其实.{0,3}(是|会|喜欢|讨厌|经常)",
    r"ta.{0,6}应该是",
    r"ta没(这么|那么|说过|做过)",
    r"ta.{0,4}不用(这个|这种|那样|这么)",
    r"(不像|不是|不对).{0,3}ta",
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

    client = get_llm_client()
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
    correction_content = response.choices[0].message.content or ""

    # 写入 corrections.md（持续追加，用户纠正记录是人物画像准确性的核心数据）
    corrections_path = ex_dir / "corrections.md"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    existing = ""
    if corrections_path.exists():
        existing = corrections_path.read_text(encoding="utf-8")
        count = len(re.findall(r"### Correction #", existing))
    else:
        existing = "# 纠正记录\n\n"
        count = 0

    new_correction = f"""
### Correction #{count + 1} — {timestamp}

{correction_content}

---
"""
    atomic_write(corrections_path, existing + new_correction)

    # 同时追加到 memory.md 的 Correction 记录节
    _append_to_memory(slug, count + 1, timestamp, user_msg)

    # 将人格相关纠正合并到 persona.md
    _patch_persona(slug, correction_content)

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

    if "## Correction 记录" not in content:
        content += "\n\n## Correction 记录\n"

    correction_entry = f"""
### Correction #{correction_num} — {timestamp}
- 用户原话：\"{user_msg}\"
- 详见 corrections.md
"""
    content += correction_entry
    atomic_write(memory_path, content)


def _patch_persona(slug: str, correction_content: str):
    """将纠正中的人格特征更新到 persona.md。"""
    ex_dir = get_ex_dir(slug)
    persona_path = ex_dir / "persona.md"
    if not persona_path.exists():
        return

    content = persona_path.read_text(encoding="utf-8")

    # 在 persona.md 末尾添加纠正记录节
    if "## 用户纠正补充" not in content:
        content += "\n\n## 用户纠正补充\n"

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n### {timestamp}\n{correction_content}\n"
    content += entry

    atomic_write(persona_path, content)


def _format_history(messages: list[dict]) -> str:
    """格式化对话历史为可读文本。"""
    lines = []
    for msg in messages:
        role = "我" if msg.get("role") == "user" else "ta"
        lines.append(f"{role}: {msg.get('content', '')[:200]}")
    return "\n".join(lines)
