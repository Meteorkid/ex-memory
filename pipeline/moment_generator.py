"""朋友圈生成器：基于 persona.md 生成朋友圈内容。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from config import get_llm_config, get_llm_client, get_ex_dir
from core.file_utils import atomic_write_json

logger = logging.getLogger("ex-memory")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def generate_moment(slug: str) -> str:
    """为指定镜像生成一条朋友圈。

    Args:
        slug: 前任代号

    Returns:
        生成的朋友圈内容

    Raises:
        FileNotFoundError: 缺少 persona.md
        RuntimeError: 未配置 LLM
    """
    ex_dir = get_ex_dir(slug)
    persona_path = ex_dir / "persona.md"
    if not persona_path.exists():
        raise FileNotFoundError("缺少 persona.md")

    cfg = get_llm_config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置 LLM API Key")

    persona_content = persona_path.read_text(encoding="utf-8")
    moment_prompt = (PROMPTS_DIR / "moment.md").read_text(encoding="utf-8")

    client = get_llm_client()
    response = client.chat.completions.create(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": moment_prompt},
            {"role": "user", "content": f"请根据以下人格画像生成一条朋友圈：\n\n{persona_content}"},
        ],
        temperature=0.9,
    )
    content = response.choices[0].message.content or ""

    moments_path = ex_dir / "moments.json"
    moments = json.loads(moments_path.read_text(encoding="utf-8")) if moments_path.exists() else []
    moments.append({
        "id": f"m{len(moments)+1}",
        "content": content,
        "created_at": datetime.now().isoformat(),
    })
    atomic_write_json(moments_path, moments)
    logger.info("朋友圈已生成: %s", slug)
    return content
