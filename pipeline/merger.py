"""增量合并：将新素材 merge 进现有的 memory.md 和 persona.md，自动备份旧版本。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from config import get_llm_config, get_llm_client, get_ex_dir
from core.file_utils import atomic_write

logger = logging.getLogger("ex-memory")
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def merge_new_material(
    slug: str,
    new_materials: str,
    source_type: str = "oral",
) -> dict:
    """将新素材增量合并到现有的 memory.md 和 persona.md。

    策略：追加式合并 —— 保留现有内容，LLM 只产出新增/修订的增量部分，
    而非重新生成整个文件，避免 LLM 幻觉截断已有记忆。

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

    client = get_llm_client()
    ex_dir = get_ex_dir(slug)

    merger_prompt = (PROMPTS_DIR / "merger.md").read_text(encoding="utf-8")

    memory_path = ex_dir / "memory.md"
    persona_path = ex_dir / "persona.md"

    existing_memory = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    existing_persona = persona_path.read_text(encoding="utf-8") if persona_path.exists() else ""

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # --- 自动备份 ---
    try:
        from core.version_manager import backup as do_backup
        do_backup(slug)
        logger.info("合并前自动备份完成: %s", slug)
    except Exception as e:
        logger.warning("自动备份失败（继续合并）: %s", e)

    def _merge_one(target_path: Path, existing: str, task_hint: str) -> str:
        """调用 LLM 生成增量并追加到目标文件，返回增量文本。"""
        user_content = f"""## 现有 {target_path.name}（请完整保留）

{existing}

## 新增素材（来源：{source_type}，日期：{timestamp}）

{new_materials}

请输出要追加到 {target_path.name} 末尾的{task_hint}（Markdown 格式），不要重复已有内容。"""
        response = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": merger_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.5,
        )
        delta = (response.choices[0].message.content or "").strip()
        if delta:
            merged = existing.rstrip() + f"\n\n---\n## 更新 — {timestamp}\n{delta}\n"
            atomic_write(target_path, merged)
            logger.info("%s 增量更新: %d 字符", target_path.name, len(delta))
        else:
            logger.warning("%s 未生成有效增量，跳过写入", target_path.name)
        return delta

    memory_delta = _merge_one(memory_path, existing_memory, "新内容")
    persona_delta = _merge_one(persona_path, existing_persona, "新观察")

    # 重新生成 SKILL.md
    from pipeline.skill_combiner import write_skill
    write_skill(slug)

    # 更新 meta.json
    meta_path = ex_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["updated_at"] = datetime.now().isoformat()
        meta["pipeline_state"] = "merged"
        atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2))

    return {
        "memory_updated": bool(memory_delta),
        "persona_updated": bool(persona_delta),
        "skill_regenerated": True,
        "memory_delta_len": len(memory_delta),
        "persona_delta_len": len(persona_delta),
    }
