"""增量合并：将新素材 merge 进现有的 memory.md 和 persona.md。"""

import json
from datetime import datetime
from pathlib import Path
from openai import OpenAI
from config import get_llm_config, get_ex_dir

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def merge_new_material(
    slug: str,
    new_materials: str,
    source_type: str = "oral",
) -> dict:
    """将新素材增量合并到现有的 memory.md 和 persona.md。

    Args:
        slug: 前任代号
        new_materials: 新的原材料摘要
        source_type: 来源类型（wechat/oral/photo）

    Returns:
        包含更新状态的字典
    """
    cfg = get_llm_config()
    if not cfg["api_key"]:
        return {"error": "未配置 LLM API Key"}

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    ex_dir = get_ex_dir(slug)

    merger_prompt = (PROMPTS_DIR / "merger.md").read_text(encoding="utf-8")

    # 读取现有文件
    memory_path = ex_dir / "memory.md"
    persona_path = ex_dir / "persona.md"

    existing_memory = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    existing_persona = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

    timestamp = datetime.now().strftime("%Y-%m-%d")

    # Step 1: 合并 memory.md
    memory_user_content = f"""## 现有 memory.md

{existing_memory}

## 新增素材（来源：{source_type}，日期：{timestamp}）

{new_materials}

请按照 merger.md 的原则，将新增素材增量合并到现有 memory.md 中。
输出完整的更新后 memory.md 内容。"""

    memory_response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": merger_prompt},
            {"role": "user", "content": memory_user_content},
        ],
        temperature=0.5,
    )
    updated_memory = memory_response.choices[0].message.content

    # Step 2: 合并 persona.md
    persona_user_content = f"""## 现有 persona.md

{existing_persona}

## 新增素材（来源：{source_type}，日期：{timestamp}）

{new_materials}

请按照 merger.md 的原则，将新增素材增量合并到现有 persona.md 中。
输出完整的更新后 persona.md 内容。"""

    persona_response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": merger_prompt},
            {"role": "user", "content": persona_user_content},
        ],
        temperature=0.5,
    )
    updated_persona = persona_response.choices[0].message.content

    # 写入文件
    memory_path.write_text(updated_memory, encoding="utf-8")
    persona_path.write_text(updated_persona, encoding="utf-8")

    # 重新生成 SKILL.md
    from pipeline.skill_combiner import write_skill
    write_skill(slug)

    # 更新 meta.json
    meta_path = ex_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["updated_at"] = datetime.now().isoformat()
        meta["pipeline_state"] = "merged"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "memory_updated": True,
        "persona_updated": True,
        "skill_regenerated": True,
    }
