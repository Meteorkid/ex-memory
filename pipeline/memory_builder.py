"""调用 LLM 生成 memory.md。"""

from pathlib import Path
from config import get_llm_config, get_llm_client

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def build_memory(slug: str, materials_summary: str, *, source_grounded: bool = False) -> str:
    """生成 memory.md 内容。

    Args:
        slug: 前任代号
        materials_summary: 原材料摘要

    Returns:
        memory.md 的完整内容
    """
    cfg = get_llm_config()
    client = get_llm_client()

    suffix = "_source_grounded" if source_grounded else ""
    analyzer_prompt = (PROMPTS_DIR / f"memory_analyzer{suffix}.md").read_text(encoding="utf-8")
    builder_prompt = (PROMPTS_DIR / f"memory_builder{suffix}.md").read_text(encoding="utf-8")

    # Step 1: 分析关系记忆
    analysis_response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": analyzer_prompt},
            {
                "role": "user",
                "content": (
                    f"请依据以下材料整理有来源的记忆，区分证据、推断与未知：\n\n{materials_summary}"
                    if source_grounded else
                    f"请分析以下原材料，提取关系记忆：\n\n{materials_summary}"
                ),
            },
        ],
        temperature=0.7,
    )
    analysis = analysis_response.choices[0].message.content

    # Step 2: 生成 memory.md
    build_response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": builder_prompt},
            {
                "role": "user",
                "content": (
                    f"请核对原始材料后生成 memory.md，保留证据与未知的区别。\n\n"
                    f"## 原始材料\n{materials_summary}\n\n## 分析草稿\n{analysis}"
                    if source_grounded else
                    f"请根据以下分析结果生成 memory.md：\n\n{analysis}"
                ),
            },
        ],
        temperature=0.7,
    )

    return build_response.choices[0].message.content
