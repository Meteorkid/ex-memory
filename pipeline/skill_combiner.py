"""合并 memory.md + persona.md → SKILL.md。"""

import logging
from core.mirror_store import mirror_store

logger = logging.getLogger("ex-memory")


def combine(slug: str, owner=None) -> str:
    """读取 exes/{owner}/{slug}/ 或 exes/{slug}/ 下的 memory.md 和 persona.md，生成 SKILL.md。

    Returns:
        SKILL.md 的完整内容
    """
    store = mirror_store(slug, owner)

    if not store.exists("meta.json"):
        raise FileNotFoundError(f"meta.json 不存在: {store.path('meta.json')}")

    meta = store.read_json("meta.json")

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

    memory_content = (
        store.read_text("memory.md") if store.exists("memory.md") else ""
    )
    persona_content = (
        store.read_text("persona.md") if store.exists("persona.md") else ""
    )

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


def write_skill(slug: str, owner=None):
    """生成并写入 SKILL.md。"""
    content = combine(slug, owner)
    store = mirror_store(slug, owner)
    store.write_text("SKILL.md", content)
    logger.info("已生成 %s", store.path("SKILL.md"))
    return content
