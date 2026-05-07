"""调用 LLM 生成 memory.md。"""

from pathlib import Path
from openai import OpenAI
from config import get_llm_config

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def build_memory(slug: str, materials_summary: str) -> str:
    """生成 memory.md 内容。

    Args:
        slug: 前任代号
        materials_summary: 原材料摘要

    Returns:
        memory.md 的完整内容
    """
    cfg = get_llm_config()
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])

    analyzer_prompt = (PROMPTS_DIR / "memory_analyzer.md").read_text(encoding="utf-8")
    builder_prompt = (PROMPTS_DIR / "memory_builder.md").read_text(encoding="utf-8")

    # Step 1: 分析关系记忆
    analysis_response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": analyzer_prompt},
            {"role": "user", "content": f"请分析以下原材料，提取关系记忆：\n\n{materials_summary}"},
        ],
        temperature=0.7,
    )
    analysis = analysis_response.choices[0].message.content

    # Step 2: 生成 memory.md
    build_response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": builder_prompt},
            {"role": "user", "content": f"请根据以下分析结果生成 memory.md：\n\n{analysis}"},
        ],
        temperature=0.7,
    )

    return build_response.choices[0].message.content
