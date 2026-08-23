"""自动化蒸馏调度：从原始数据到可对话的完整流水线。"""

import json
import logging
from datetime import datetime
from pathlib import Path
from prompt_toolkit import prompt as pt_prompt

from config import get_ex_dir, ensure_ex_dirs, get_embedding_config, get_llm_config, get_collection_name
from core.file_utils import atomic_write, atomic_write_json
from core.version_manager import backup as version_backup
from memory.embedder import Embedder
from memory.vector_store import VectorStore
from memory.chunker import Chunker
from pipeline.memory_builder import build_memory
from pipeline.persona_builder import build_persona
from pipeline.skill_combiner import write_skill

logger = logging.getLogger("ex-memory")

# 管线步骤顺序
PIPELINE_STEPS = ["import", "distill_memory", "distill_persona", "skill"]


def _save_failed_state(ex_dir: Path, step: str, error: Exception):
    """保存失败状态到 meta.json。"""
    meta_path = ex_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {}
    meta["pipeline_state"] = "failed"
    meta["failed_step"] = step
    meta["error"] = str(error)[:500]
    meta["updated_at"] = datetime.now().isoformat()
    atomic_write_json(meta_path, meta)


def run_create_flow(slug: str = None):
    """执行完整的创建流程。

    slug: 恢复已有镜像的创建流程（从失败步骤继续）
    """

    # ===== 检查是否为恢复模式 =====
    existing_meta = None
    if slug:
        ex_dir = get_ex_dir(slug)
        meta_path = ex_dir / "meta.json"
        if meta_path.exists():
            existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if existing_meta.get("pipeline_state") == "failed" and existing_meta.get("failed_step"):
                failed_step = existing_meta["failed_step"]
                print("\n=== 恢复创建流程 ===")
                print(f"镜像 [{existing_meta['name']}] 上次在 [{failed_step}] 步骤失败")
                print(f"错误信息: {existing_meta.get('error', '未知')}")
                confirm = pt_prompt("从失败步骤继续？(y/n): ").strip().lower()
                if confirm not in ("y", "yes", "是", ""):
                    print("已取消")
                    return
            else:
                existing_meta = None  # 非失败状态，走全新流程

    # ===== Step 1: 基础信息录入 =====
    if existing_meta:
        # 恢复模式：从 meta.json 读取已有信息
        name = existing_meta["name"]
        slug = existing_meta["slug"]
        basic_info = existing_meta.get("profile", {}).get("basic_info", "")
        personality = existing_meta.get("profile", {}).get("personality", "")
        ex_dir = get_ex_dir(slug)
        print(f"\n恢复镜像 [{name}] 的创建流程...")
    else:
        # 全新模式
        print("\n=== 前任记忆智能体 — 创建向导 ===\n")

        name = pt_prompt("给ta起个代号（昵称/备注名/外号）: ").strip()
        if not name:
            print("错误：代号不能为空")
            return

        slug = name.lower().replace(" ", "_")

        print("\n一句话介绍一下？（在一起多久、分手多久、ta做什么的）")
        print("示例：在一起两年 分手半年 互联网产品经理 上海")
        basic_info = pt_prompt("基本信息（可跳过）: ").strip()

        print("\n用一句话描述ta的性格？（MBTI、星座、性格标签）")
        print("示例：ENFP 双子座 话很多 永远在社交 但深夜会突然emo")
        personality = pt_prompt("性格画像（可跳过）: ").strip()

        # 汇总确认
        print("\n=== 信息确认 ===")
        print(f"  代号：{name}")
        print(f"  基本信息：{basic_info or '（未填写）'}")
        print(f"  性格画像：{personality or '（未填写）'}")

        confirm = pt_prompt("\n确认？(y/n): ").strip().lower()
        if confirm not in ("y", "yes", "是", ""):
            print("已取消")
            return

        # 创建目录
        ex_dir = ensure_ex_dirs(slug)

        # 写入初始 meta.json
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

        # 事前备份
        try:
            version_backup(slug, "pre_create", include_chroma=True)
        except Exception:
            pass

    # 判断从哪一步开始
    failed_step = existing_meta.get("failed_step") if existing_meta else None
    steps = ["import", "distill_memory", "distill_persona", "skill"]
    start_idx = steps.index(failed_step) if failed_step in steps else 0

    materials_summary = f"代号：{name}\n基本信息：{basic_info}\n性格画像：{personality}\n\n"

    # 初始化向量库（import 步骤需要）
    emb_cfg = get_embedding_config()
    embedder = Embedder(api_key=emb_cfg["api_key"], base_url=emb_cfg["base_url"], model=emb_cfg["model"])
    chroma_dir = str(ex_dir / "chroma_db")
    collection_name = get_collection_name(slug)
    vector_store = VectorStore(persist_dir=chroma_dir, collection_name=collection_name)
    chunker = Chunker()

    # ===== Step 2: 数据源导入 =====
    if start_idx <= 0:
        print("\n=== 数据源导入 ===")
        print("回忆越多，还原度越高。")
        print("[A] 微信聊天记录导出")
        print("[B] 直接粘贴/口述")
        print("[C] 跳过（仅凭上述信息生成）")

        source_choice = pt_prompt("选择 (A/B/C): ").strip().upper()

        try:
            if source_choice == "A":
                _import_wechat(ex_dir, slug, name, vector_store, embedder, chunker)
            elif source_choice == "B":
                _import_oral(ex_dir, slug, name, vector_store, embedder, chunker)
            else:
                print("跳过数据导入。")
        except Exception as e:
            logger.error("数据导入失败: %s", e, exc_info=True)
            _save_failed_state(ex_dir, "import", e)
            print(f"数据导入失败: {e}")
            print("请修复问题后重新运行 /create 并输入代号继续。")
            return

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
    llm_cfg = get_llm_config()
    if not llm_cfg["api_key"]:
        print("警告：未配置 LLM API Key，跳过自动蒸馏。")
        print("请手动创建 memory.md 和 persona.md 后运行 /create 的后续步骤。")
        return

    if start_idx <= 1:
        print("\n=== 开始蒸馏 ===")
        print("正在生成 Relationship Memory...")
        try:
            memory_content = build_memory(slug, materials_summary)
            atomic_write(ex_dir / "memory.md", memory_content)
            print("  memory.md 已生成")
        except Exception as e:
            logger.error("生成 memory.md 失败: %s", e, exc_info=True)
            _save_failed_state(ex_dir, "distill_memory", e)
            print(f"生成 memory.md 失败: {e}")
            print("可稍后重新运行 /create 并输入代号继续。")
            return

    if start_idx <= 2:
        print("正在生成 Persona（含原话样本抽取）...")
        try:
            persona_content = build_persona(slug, materials_summary, vector_store, embedder)
            atomic_write(ex_dir / "persona.md", persona_content)
            print("  persona.md 已生成")
        except Exception as e:
            logger.error("生成 persona.md 失败: %s", e, exc_info=True)
            _save_failed_state(ex_dir, "distill_persona", e)
            print(f"生成 persona.md 失败: {e}")
            print("可稍后重新运行 /create 并输入代号继续。")
            return

    # ===== Step 4: 生成 SKILL.md =====
    if start_idx <= 3:
        print("正在生成 SKILL.md...")
        try:
            write_skill(slug)
        except Exception as e:
            logger.error("生成 SKILL.md 失败: %s", e, exc_info=True)
            _save_failed_state(ex_dir, "skill", e)
            print(f"生成 SKILL.md 失败: {e}")
            return

    # 更新 meta.json
    meta = json.loads((ex_dir / "meta.json").read_text(encoding="utf-8"))
    meta["pipeline_state"] = "completed"
    meta.pop("failed_step", None)
    meta.pop("error", None)
    meta["updated_at"] = datetime.now().isoformat()
    atomic_write_json(ex_dir / "meta.json", meta)

    # 展示摘要
    print("\n=== 创建完成！===")
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


def run_create_flow_api(
    slug: str,
    name: str,
    answers: list[str],
    resume: bool = False,
    owner_user_id=None,
) -> dict:
    """API 版创建流程：接收参数而非交互式输入，返回结果字典。

    resume: 为 True 时从上次失败步骤继续（需已有 meta.json）
    """

    try:
        ex_dir = get_ex_dir(slug) if resume else ensure_ex_dirs(slug)

        # 恢复模式：读取已有 meta
        if resume:
            meta_path = ex_dir / "meta.json"
            if not meta_path.exists():
                return {"error": "未找到创建记录，无法恢复"}
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("pipeline_state") != "failed":
                return {"error": "该镜像未处于失败状态，无需恢复"}
            failed_step = meta.get("failed_step", "")
            start_idx = PIPELINE_STEPS.index(failed_step) if failed_step in PIPELINE_STEPS else 0
            basic_info = meta.get("profile", {}).get("basic_info", "")
            personality = meta.get("profile", {}).get("personality", "")
            name = meta.get("name", name)
        else:
            basic_info = answers[0] if len(answers) > 0 else ""
            personality = answers[1] if len(answers) > 1 else ""
            start_idx = 0

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
            if owner_user_id is not None:
                meta["owner_user_id"] = owner_user_id
            atomic_write_json(ex_dir / "meta.json", meta)

            # 事前备份
            try:
                version_backup(slug, "pre_create", include_chroma=True)
            except Exception:
                pass

        materials_summary = f"代号：{name}\n基本信息：{basic_info}\n性格画像：{personality}\n\n"

        # 初始化向量库和 embedder
        emb_cfg = get_embedding_config()
        embedder = None
        vector_store = None
        if emb_cfg["api_key"]:
            embedder = Embedder(
                api_key=emb_cfg["api_key"],
                base_url=emb_cfg["base_url"],
                model=emb_cfg["model"],
            )
            vector_store = VectorStore(
                persist_dir=str(ex_dir / "chroma_db"),
                collection_name=get_collection_name(slug),
            )

        # 蒸馏
        llm_cfg = get_llm_config()
        if not llm_cfg["api_key"]:
            return {"error": "未配置 LLM API Key"}

        if start_idx <= 1:
            try:
                memory_content = build_memory(slug, materials_summary)
                atomic_write(ex_dir / "memory.md", memory_content)
            except Exception as e:
                _save_failed_state(ex_dir, "distill_memory", e)
                return {"error": f"生成 memory.md 失败: {e}"}

        if start_idx <= 2:
            try:
                persona_content = build_persona(slug, materials_summary, vector_store, embedder)
                atomic_write(ex_dir / "persona.md", persona_content)
            except Exception as e:
                _save_failed_state(ex_dir, "distill_persona", e)
                return {"error": f"生成 persona.md 失败: {e}"}

        if start_idx <= 3:
            try:
                write_skill(slug)
            except Exception as e:
                _save_failed_state(ex_dir, "skill", e)
                return {"error": f"生成 SKILL.md 失败: {e}"}

        # 读取最新 meta 并更新
        meta = json.loads((ex_dir / "meta.json").read_text(encoding="utf-8"))
        meta["pipeline_state"] = "completed"
        meta.pop("failed_step", None)
        meta.pop("error", None)
        meta["updated_at"] = datetime.now().isoformat()
        atomic_write_json(ex_dir / "meta.json", meta)

        logger.info("API 创建完成: %s", slug)
        return {"slug": slug, "name": name, "state": "completed"}

    except Exception as e:
        logger.error("API 创建失败: %s", e, exc_info=True)
        return {"error": str(e)}
