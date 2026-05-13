"""自动化蒸馏调度：从原始数据到可对话的完整流水线。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from prompt_toolkit import prompt as pt_prompt

from config import get_ex_dir, ensure_ex_dirs, get_embedding_config, get_llm_config, get_collection_name
from core.file_utils import atomic_write, atomic_write_json
from memory.embedder import Embedder
from memory.vector_store import VectorStore
from memory.chunker import Chunker
from pipeline.memory_builder import build_memory
from pipeline.persona_builder import build_persona
from pipeline.skill_combiner import write_skill

logger = logging.getLogger("ex-memory")


def run_create_flow():
    """执行完整的创建流程。"""

    # ===== Step 1: 基础信息录入 =====
    print("\n=== 前任记忆智能体 — 创建向导 ===\n")

    name = pt_prompt("给ta起个代号（昵称/备注名/外号）: ").strip()
    if not name:
        print("错误：代号不能为空")
        return

    slug = name.lower().replace(" ", "_")

    print(f"\n一句话介绍一下？（在一起多久、分手多久、ta做什么的）")
    print("示例：在一起两年 分手半年 互联网产品经理 上海")
    basic_info = pt_prompt("基本信息（可跳过）: ").strip()

    print(f"\n用一句话描述ta的性格？（MBTI、星座、性格标签）")
    print("示例：ENFP 双子座 话很多 永远在社交 但深夜会突然emo")
    personality = pt_prompt("性格画像（可跳过）: ").strip()

    # 汇总确认
    print(f"\n=== 信息确认 ===")
    print(f"  代号：{name}")
    print(f"  基本信息：{basic_info or '（未填写）'}")
    print(f"  性格画像：{personality or '（未填写）'}")

    confirm = pt_prompt("\n确认？(y/n): ").strip().lower()
    if confirm not in ("y", "yes", "是", ""):
        print("已取消")
        return

    # 创建目录
    ex_dir = ensure_ex_dirs(slug)

    # 写入 meta.json
    meta = {
        "name": name,
        "slug": slug,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "version": "v1",
        "profile": {
            "basic_info": basic_info,
            "personality": personality,
        },
        "pipeline_state": "intake_done",
    }
    atomic_write_json(ex_dir / "meta.json", meta)

    # ===== Step 2: 数据源导入 =====
    print(f"\n=== 数据源导入 ===")
    print("回忆越多，还原度越高。")
    print("[A] 微信聊天记录导出")
    print("[B] 直接粘贴/口述")
    print("[C] 跳过（仅凭上述信息生成）")

    source_choice = pt_prompt("选择 (A/B/C): ").strip().upper()
    materials_summary = f"代号：{name}\n基本信息：{basic_info}\n性格画像：{personality}\n\n"

    # 初始化向量库
    emb_cfg = get_embedding_config()
    embedder = Embedder(api_key=emb_cfg["api_key"], base_url=emb_cfg["base_url"], model=emb_cfg["model"])
    chroma_dir = str(ex_dir / "chroma_db")
    collection_name = get_collection_name(slug)
    vector_store = VectorStore(persist_dir=chroma_dir, collection_name=collection_name)
    chunker = Chunker()

    if source_choice == "A":
        _import_wechat(ex_dir, slug, name, vector_store, embedder, chunker)
    elif source_choice == "B":
        _import_oral(ex_dir, slug, name, vector_store, embedder, chunker)
    else:
        print("跳过数据导入。")

    # 构建材料摘要（从已入库的数据中采样）
    try:
        sample_results = vector_store.search("日常对话", embedder, top_k=20)
        if sample_results:
            materials_summary += "\n## 聊天记录样本\n"
            for r in sample_results[:10]:
                materials_summary += f"- {r.get('display_text', '')[:200]}\n"
    except Exception:
        pass

    # ===== Step 3: 蒸馏 =====
    print(f"\n=== 开始蒸馏 ===")

    llm_cfg = get_llm_config()
    if not llm_cfg["api_key"]:
        print("警告：未配置 LLM API Key，跳过自动蒸馏。")
        print("请手动创建 memory.md 和 persona.md 后运行 /create 的后续步骤。")
        return

    print("正在生成 Relationship Memory...")
    memory_content = build_memory(slug, materials_summary)
    atomic_write(ex_dir / "memory.md", memory_content)
    print("  memory.md 已生成")

    print("正在生成 Persona（含原话样本抽取）...")
    persona_content = build_persona(slug, materials_summary, vector_store, embedder)
    atomic_write(ex_dir / "persona.md", persona_content)
    print("  persona.md 已生成")

    # ===== Step 4: 生成 SKILL.md =====
    print("正在生成 SKILL.md...")
    write_skill(slug)

    # 更新 meta.json
    meta["pipeline_state"] = "completed"
    meta["updated_at"] = datetime.now().isoformat()
    atomic_write_json(ex_dir / "meta.json", meta)

    # 展示摘要
    print(f"\n=== 创建完成！===")
    print(f"前任 [{name}] 的数字镜像已就绪。")
    print(f"输入 /{slug} 开始对话。\n")


def _import_wechat(ex_dir: Path, slug: str, name: str, vector_store, embedder, chunker):
    """导入微信聊天记录。"""
    from parsers.wechat_parser import parse as wechat_parse

    file_path = pt_prompt("微信聊天记录文件路径: ").strip()
    if not file_path:
        print("未提供文件，跳过。")
        return

    file_path = file_path.strip('"').strip("'")
    if not Path(file_path).exists():
        print(f"文件不存在: {file_path}")
        return

    print("正在解析聊天记录...")
    messages = wechat_parse(file_path, target_name=name)
    print(f"  解析完成：{len(messages)} 条消息")

    if not messages:
        print("  未提取到有效消息。")
        return

    print("正在切片入库...")
    chunks = chunker.chunk_messages(messages, source="wechat", chat_id=f"wechat_{slug}")
    print(f"  切片完成：{len(chunks)} 个 chunks")

    if chunks:
        vector_store.ingest(chunks, embedder)
        print(f"  入库完成：{vector_store.count()} 条记录")


def _import_oral(ex_dir: Path, slug: str, name: str, vector_store, embedder, chunker):
    """导入口述/粘贴文本。"""
    from parsers.oral_parser import parse as oral_parse

    print("\n请粘贴你想记录的内容（输入空行结束）：")
    lines = []
    while True:
        try:
            line = input()
            if line == "":
                break
            lines.append(line)
        except EOFError:
            break

    text = "\n".join(lines)
    if not text.strip():
        print("未输入内容，跳过。")
        return

    messages = oral_parse(text, target_name=name)
    print(f"  解析完成：{len(messages)} 条消息")

    chunks = chunker.chunk_text(text, source="oral")
    if chunks:
        vector_store.ingest(chunks, embedder)
        print(f"  入库完成：{vector_store.count()} 条记录")


def run_create_flow_api(slug: str, name: str, answers: list[str]) -> dict:
    """API 版创建流程：接收参数而非交互式输入，返回结果字典。"""

    try:
        basic_info = answers[0] if len(answers) > 0 else ""
        personality = answers[1] if len(answers) > 1 else ""

        ex_dir = ensure_ex_dirs(slug)

        meta = {
            "name": name,
            "slug": slug,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "version": "v1",
            "profile": {
                "basic_info": basic_info,
                "personality": personality,
            },
            "pipeline_state": "intake_done",
        }
        atomic_write_json(ex_dir / "meta.json", meta)

        materials_summary = f"代号：{name}\n基本信息：{basic_info}\n性格画像：{personality}\n\n"

        # 初始化向量库（空库，后续 /update 追加数据）
        emb_cfg = get_embedding_config()
        if emb_cfg["api_key"]:
            embedder = Embedder(
                api_key=emb_cfg["api_key"],
                base_url=emb_cfg["base_url"],
                model=emb_cfg["model"],
            )
            VectorStore(
                persist_dir=str(ex_dir / "chroma_db"),
                collection_name=get_collection_name(slug),
            )

        # 蒸馏
        llm_cfg = get_llm_config()
        if not llm_cfg["api_key"]:
            return {"error": "未配置 LLM API Key"}

        memory_content = build_memory(slug, materials_summary)
        atomic_write(ex_dir / "memory.md", memory_content)

        persona_content = build_persona(slug, materials_summary, None, None)
        atomic_write(ex_dir / "persona.md", persona_content)

        write_skill(slug)

        meta["pipeline_state"] = "completed"
        meta["updated_at"] = datetime.now().isoformat()
        atomic_write_json(ex_dir / "meta.json", meta)

        logger.info("API 创建完成: %s", slug)
        return {"slug": slug, "name": name, "state": "completed"}

    except Exception as e:
        logger.error("API 创建失败: %s", e, exc_info=True)
        return {"error": str(e)}
