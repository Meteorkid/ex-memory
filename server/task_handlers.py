"""长任务的处理器实现。

这些逻辑原本直接写在路由里同步执行。搬到这里后路由只负责收参数、提交
任务、返回 task_id，真正的工作在 worker 线程里做。
"""

import logging
import hashlib
import json
import shutil
from pathlib import Path

from core.tasks import TaskProgress, register

logger = logging.getLogger("ex-memory")

TASK_IMPORT = "import_chat"
TASK_REFLECT = "reflect"
TASK_MOMENT = "generate_moment"
TASK_BACKUP = "backup"
TASK_PROACTIVE = "proactive_tick"


@register(TASK_IMPORT)
def handle_import(
    progress: TaskProgress, *, slug: str, owner: int, source_path: str, target_name: str
) -> dict:
    """解析入库，并用实际聊天材料完成首次镜像生成。

    源文件由路由落到暂存区，这里读完即删——原始聊天记录是最敏感的那份
    数据，不该在磁盘上多留一分钟。
    """
    from config import get_collection_name, get_embedding_config, resolve_ex_dir
    from core.exe_access import assert_exe_access
    from core.mirror_store import mirror_store
    from memory.embedder import Embedder
    from memory.vector_store import VectorStore
    from pipeline.orchestrator import import_creation_lock, run_create_flow_api
    from server.routes import _invalidate_engine

    path = Path(source_path)
    try:
        assert_exe_access(slug, owner)
        if not path.is_file():
            raise ValueError("原始上传文件已清理，请重新选择文件上传")
        with import_creation_lock(slug, owner):
            store = mirror_store(slug, owner)
            meta = store.read_json("meta.json")
            first_generation = (
                meta.get("creation_source") == "chat_import"
                and meta.get("pipeline_state") != "completed"
            )
            digest = hashlib.sha256()
            digest.update((target_name.strip() + path.suffix.lower()).encode())
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            import_id = digest.hexdigest()
            receipt = meta.get("imported_files", {}).get(import_id)
            if receipt is None:
                progress.update(5, "准备解析")
                emb_cfg = get_embedding_config()
                if not emb_cfg["api_key"]:
                    raise RuntimeError("未配置 Embedding API Key")

                ex_dir = resolve_ex_dir(slug, owner)
                embedder = Embedder(
                    api_key=emb_cfg["api_key"],
                    base_url=emb_cfg["base_url"],
                    model=emb_cfg["model"],
                )
                vector_store = VectorStore(
                    persist_dir=str(ex_dir / "chroma_db"),
                    collection_name=get_collection_name(slug),
                )
                progress.update(20, "解析聊天记录")
                suffix = path.suffix.lower()
                if suffix in (".mht", ".mhtml", ".txt"):
                    from memory.ingest import ingest_qq_file as ingest_file
                else:
                    from memory.ingest import ingest_wechat_file as ingest_file
                try:
                    messages, chunks = ingest_file(
                        str(path), slug, target_name, vector_store, embedder,
                        owner=owner, import_id=import_id, require_target=True,
                    )
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ValueError("文件内容或编码无效，请重新导出 UTF-8 聊天记录后上传") from exc
                if not messages or not chunks:
                    raise ValueError("未提取到有效聊天消息，请检查文件内容和格式")
                receipt = {"messages": len(messages), "chunks": chunks}

                def save_receipt(current):
                    current.setdefault("imported_files", {})[import_id] = receipt
                    return current

                store.locked_update_json("meta.json", {}, save_receipt)
            if first_generation:
                progress.update(55, "正在根据聊天记录生成记忆与人物画像")
                result = run_create_flow_api(
                    slug, meta.get("name", slug), [], resume=True,
                    owner_user_id=owner, from_import=True,
                )
                if result.get("error"):
                    raise RuntimeError(result["error"])
            progress.update(95, "镜像已就绪" if first_generation else "入库完成")
            return {
                **receipt,
                "state": store.read_json("meta.json").get("pipeline_state", "unknown"),
                "message": (
                    "聊天记录导入完成，记忆与人物画像已生成，可以开始模拟对话"
                    if first_generation else
                    f"导入完成：解析 {receipt['messages']} 条消息，入库 {receipt['chunks']} 个切片"
                ),
            }
    finally:
        _invalidate_engine(slug)
        shutil.rmtree(path.parent, ignore_errors=True)


@register(TASK_REFLECT)
def handle_reflect(progress: TaskProgress, *, slug: str, owner: int) -> dict:
    from pipeline.reflector import run_reflection

    progress.update(10, "正在分析关系")
    run_reflection(slug, owner=owner)
    return {"message": "反思完成"}


@register(TASK_MOMENT)
def handle_moment(progress: TaskProgress, *, slug: str, owner: int) -> dict:
    from pipeline.moment_generator import generate_moment

    progress.update(10, "正在生成")
    generate_moment(slug, owner=owner)
    return {"message": "朋友圈已生成"}


@register(TASK_BACKUP)
def handle_backup(
    progress: TaskProgress, *, slug: str, owner: int, version_name: str = ""
) -> dict:
    from core.version_manager import backup

    progress.update(10, "正在打包")
    version = backup(slug, version_name, owner=owner)
    return {"version": version, "message": f"备份成功: {version}"}


@register(TASK_PROACTIVE)
def handle_proactive_tick(progress: TaskProgress, *, slug: str, owner: int) -> dict:
    """为一个镜像判定并生成主动消息。

    生成走同一个引擎，所以人格、风格、时间感知全都一致——另写一套模板会让
    主动消息一眼看出是系统发的。
    """
    from core.conversation_store import load_jsonl_messages
    from core.proactive import compose, decide_trigger, queue_message
    from core.factory import create_engine_and_store

    history = load_jsonl_messages(slug, owner)
    last_seen = history[-1].get("created_at") if history else None

    trigger = decide_trigger(slug, owner, owner=owner, last_seen_iso=last_seen)
    if trigger is None:
        return {"sent": False, "reason": "当前不满足触发条件"}

    progress.update(40, "正在生成")
    engine, _store, _embedder = create_engine_and_store(slug, owner=owner)
    content = compose(engine, trigger)
    message_id = queue_message(slug, owner, trigger, content, owner=owner)
    return {
        "sent": message_id is not None,
        "trigger": trigger,
        "message_id": message_id,
    }
