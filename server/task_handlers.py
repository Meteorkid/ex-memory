"""长任务的处理器实现。

这些逻辑原本直接写在路由里同步执行。搬到这里后路由只负责收参数、提交
任务、返回 task_id，真正的工作在 worker 线程里做。
"""

import logging
import shutil
from pathlib import Path

from core.tasks import TaskProgress, register

logger = logging.getLogger("ex-memory")

TASK_IMPORT = "import_chat"
TASK_REFLECT = "reflect"
TASK_MOMENT = "generate_moment"
TASK_BACKUP = "backup"


@register(TASK_IMPORT)
def handle_import(
    progress: TaskProgress, *, slug: str, owner: int, source_path: str, target_name: str
) -> dict:
    """解析聊天记录并入向量库。

    源文件由路由落到暂存区，这里读完即删——原始聊天记录是最敏感的那份
    数据，不该在磁盘上多留一分钟。
    """
    from config import get_collection_name, get_embedding_config, resolve_ex_dir
    from memory.embedder import Embedder
    from memory.vector_store import VectorStore
    from parsers.wechat_parser import detect_format

    path = Path(source_path)
    try:
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
        if suffix in (".mht", ".mhtml"):
            from memory.ingest import ingest_qq_file

            messages, chunks = ingest_qq_file(
                str(path), slug, target_name, vector_store, embedder, owner=owner
            )
        elif suffix == ".txt" and detect_format(str(path)) == "plaintext":
            from memory.ingest import ingest_qq_file

            messages, chunks = ingest_qq_file(
                str(path), slug, target_name, vector_store, embedder, owner=owner
            )
        else:
            from memory.ingest import ingest_wechat_file

            messages, chunks = ingest_wechat_file(
                str(path), slug, target_name, vector_store, embedder, owner=owner
            )

        progress.update(95, "入库完成")
        if not messages:
            return {"messages": 0, "chunks": 0, "message": "未提取到有效消息"}
        return {
            "messages": len(messages),
            "chunks": chunks,
            "message": f"导入完成：解析 {len(messages)} 条消息，入库 {chunks} 个切片",
        }
    finally:
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
