"""/rebuild-vectors — 用当前分块参数重建指定镜像的向量库。

分块参数（CHUNK_TURNS/CHUNK_OVERLAP）调整后，旧向量仍按旧参数切片，
与新增向量混存会污染检索。本命令提供手动的向量重建入口：清空旧 collection
后，按源聊天文件重新解析、按当前参数重新切片并入库。

镜像对导入源文件不持久化（导入后即删临时文件），因此重建必须以源文件为输入；
一个镜像若累计导入了多份文件，需对每份依次执行重建。用法：

    /rebuild-vectors {slug} {聊天记录文件}
"""

import logging
from pathlib import Path

from config import (
    CHUNK_TURNS,
    CHUNK_OVERLAP,
    resolve_ex_dir,
    get_collection_name,
)
from core.validation import validate_slug
from commands import register

logger = logging.getLogger("ex-memory")


def _rebuild(slug: str, src_path: Path) -> str:
    """重建单个镜像的向量库，返回对人友好的结果消息。"""
    from config import get_embedding_config
    from memory.embedder import Embedder
    from memory.vector_store import VectorStore

    ex_dir = resolve_ex_dir(slug)
    emb_cfg = get_embedding_config()
    if not emb_cfg["api_key"]:
        raise RuntimeError("未配置 Embedding API Key，无法重建向量库")

    embedder = Embedder(
        api_key=emb_cfg["api_key"],
        base_url=emb_cfg["base_url"],
        model=emb_cfg["model"],
    )
    vector_store = VectorStore(
        persist_dir=str(ex_dir / "chroma_db"),
        collection_name=get_collection_name(slug),
    )
    # 先清空旧 collection，避免旧参数切片与新参数切片混存
    vector_store.delete_collection()
    vector_store = VectorStore(
        persist_dir=str(ex_dir / "chroma_db"),
        collection_name=get_collection_name(slug),
    )

    # 按扩展名选择解析器，与 REST /import 保持同一套判定
    ext = src_path.suffix.lower()
    if ext in (".mht", ".mhtml"):
        from memory.ingest import ingest_qq_file

        messages, chunk_count = ingest_qq_file(
            str(src_path), slug, "", vector_store, embedder
        )
    elif ext == ".txt":
        from parsers.wechat_parser import detect_format
        from memory.ingest import ingest_qq_file, ingest_wechat_file

        if detect_format(str(src_path)) == "plaintext":
            messages, chunk_count = ingest_qq_file(
                str(src_path), slug, "", vector_store, embedder
            )
        else:
            messages, chunk_count = ingest_wechat_file(
                str(src_path), slug, "", vector_store, embedder
            )
    else:
        from memory.ingest import ingest_wechat_file

        messages, chunk_count = ingest_wechat_file(
            str(src_path), slug, "", vector_store, embedder
        )

    return (
        f"重建完成：共 {len(messages)} 条消息，按 {CHUNK_TURNS} 轮/重叠 {CHUNK_OVERLAP} "
        f"入库 {chunk_count} 个切片"
    )


def cmd_vector_rebuild(args: str) -> None:
    """CLI 入口：/rebuild-vectors {slug} {聊天记录文件}"""
    parts = args.strip().split(None, 1)
    if len(parts) != 2:
        print("用法: /rebuild-vectors {slug} {聊天记录文件}")
        return
    slug, src = parts[0], parts[1].strip()
    try:
        validate_slug(slug)
    except ValueError as e:
        print(f"非法 slug: {e}")
        return
    src_path = Path(src)
    if not src_path.exists():
        print(f"源文件不存在: {src_path}")
        return
    ex_dir = resolve_ex_dir(slug)
    if not ex_dir.exists():
        print(f"镜像 [{slug}] 不存在")
        return
    try:
        print(_rebuild(slug, src_path))
    except RuntimeError as e:
        print(str(e))
    except Exception as e:  # 网络/解析/入库异常不应让 CLI 崩溃
        print(f"重建失败: {e}")
        logger.exception("镜像 [%s] 向量重建失败", slug)


register("rebuild-vectors", cmd_vector_rebuild)
