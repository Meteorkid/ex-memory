"""关系反思分析器。

对关系记忆进行 7 维度深度反思，生成 reflections.md。
"""

import logging
from pathlib import Path
from config import get_llm_config, get_llm_client, get_ex_dir
from core.file_utils import atomic_write

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
logger = logging.getLogger("ex-memory")


def run_reflection(slug: str) -> str:
    """运行关系反思分析。

    Args:
        slug: 前任代号

    Returns:
        反思分析文本

    Raises:
        FileNotFoundError: 缺少 memory.md
        RuntimeError: LLM 调用失败
    """
    cfg = get_llm_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置 LLM API Key")

    ex_dir = get_ex_dir(slug)
    memory_path = ex_dir / "memory.md"
    if not memory_path.exists():
        raise FileNotFoundError(f"镜像 [{slug}] 缺少 memory.md，请先完成创建流程")

    reflect_prompt = (PROMPTS_DIR / "reflect.md").read_text(encoding="utf-8")
    memory_content = memory_path.read_text(encoding="utf-8")

    client = get_llm_client()
    response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": reflect_prompt},
            {"role": "user", "content": f"请根据以下关系记忆进行反思分析：\n\n{memory_content}"},
        ],
        temperature=0.7,
    )
    reflection = response.choices[0].message.content
    atomic_write(ex_dir / "reflections.md", reflection)
    logger.info("反思完成: %s", slug)
    return reflection
