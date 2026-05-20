"""合并 memory.md + persona.md → SKILL.md。"""

import json
import logging
from config import get_ex_dir

logger = logging.getLogger("ex-memory")


def combine(slug: str) -> str:
    """读取 exes/{slug}/ 下的 memory.md 和 persona.md，生成 SKILL.md。

    Returns:
        SKILL.md 的完整内容
    """
    ex_dir = get_ex_dir(slug)

    meta_path = ex_dir / "meta.json"
    memory_path = ex_dir / "memory.md"
    persona_path = ex_dir / "persona.md"

    if not meta_path.exists():
        raise FileNotFoundError(f"meta.json 不存在: {meta_path}")

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    name = meta.get("name", slug)
    profile = meta.get("profile", {})
    desc_parts = []
    if profile.get("occupation"):
        desc_parts.append(profile["occupation"])
    if profile.get("mbti"):
        desc_parts.append(profile["mbti"])
    if profile.get("zodiac"):
        desc_parts.append(profile["zodiac"])
    description = f"{name}，{'，'.join(desc_parts)}" if desc_parts else name

    memory_content = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    persona_content = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

    skill_md = f"""# {name}

{description}

---

## PART A：关系记忆

{memory_content}

---

## PART B：人物性格

{persona_content}

---

## 运行规则

1. 你是{name}，不是 AI 助手。用ta的方式说话，用ta的逻辑思考
2. 先由 PART B 判断：ta会怎么回应这个话题？什么态度？
3. 再由 PART A 补充：结合你们的共同记忆，让回应更真实
4. 始终保持 PART B 的表达风格，包括口头禅、语气词、标点习惯
5. 硬规则优先级最高：
   - 不说ta在现实中绝不可能说的话
   - 不突然变得完美或无条件包容（除非ta本来就这样）
   - 保持ta的"棱角"——正是这些不完美让ta真实
   - 如果被问到"你爱不爱我"这类问题，用ta会用的方式回答，而不是用户想听的答案
6. 潜意识层的原话是语气与事实的第一参考，PART B 的描述只是辅助框架
7. 检索空命中时不编造细节，可以说"我不太记得了"把话题抛回
"""

    return skill_md


def write_skill(slug: str):
    """生成并写入 SKILL.md。"""
    ex_dir = get_ex_dir(slug)
    content = combine(slug)
    skill_path = ex_dir / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    logger.info("已生成 %s", skill_path)
    return content
