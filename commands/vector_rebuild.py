"""/rebuild-vectors — 用当前分块参数重建指定镜像的向量库。

分块参数（CHUNK_TURNS/CHUNK_OVERLAP）调整后，旧向量仍按旧参数切片，
与新增向量混存会污染检索。本命令提供手动的向量重建入口：清空旧 collection
后，按源聊天文件重新解析、按当前参数重新切片并入库。

优先从语料归档（corpus.jsonl）重放，不需要原始文件——归档是在导入时
落下的脱敏消息。只有归档缺失的存量镜像才需要提供源文件。用法：

    /rebuild-vectors {slug}                  # 从语料归档重建
    /rebuild-vectors {slug} {聊天记录文件}    # 归档缺失时从源文件重建
"""

import logging
from pathlib import Path

from config import find_ex_dir_with_owner
from core.validation import validate_slug
from commands import register

logger = logging.getLogger("ex-memory")


def _rebuild_from_corpus(slug: str, owner=None) -> str:
    """从语料归档重放重建向量库。

    归档是导入时落下的脱敏消息，重放它不需要用户再找一遍原始文件——
    这正是「换 embedding 模型 / 调分块参数 / 迁移向量库」得以执行的前提。
    """
    from config import CHUNK_OVERLAP, CHUNK_TURNS
    from core.corpus_store import load_messages
    from memory.chunker import Chunker

    messages = load_messages(slug, owner)
    if not messages:
        raise RuntimeError("语料归档为空")

    vector_store, embedder = _fresh_store(slug, owner)
    chunker = Chunker()
    by_source: dict[str, list[dict]] = {}
    for message in messages:
        by_source.setdefault(message.get("_source", "wechat"), []).append(message)

    total = 0
    for source, group in by_source.items():
        chunks = chunker.chunk_messages(
            group, source=source, chat_id=f"{source}_{slug}"
        )
        if chunks:
            vector_store.ingest(chunks, embedder)
            total += len(chunks)

    return (
        f"重建完成：重放 {len(messages)} 条归档消息，"
        f"按 {CHUNK_TURNS} 轮/重叠 {CHUNK_OVERLAP} 入库 {total} 个切片"
    )


def _fresh_store(slug: str, owner=None):
    """清空旧 collection 后返回全新的 store 与 embedder。"""
    from config import get_collection_name, get_embedding_config, resolve_ex_dir
    from memory.embedder import Embedder
    from memory.vector_store import VectorStore

    emb_cfg = get_embedding_config()
    if not emb_cfg["api_key"]:
        raise RuntimeError("未配置 Embedding API Key，无法重建向量库")

    ex_dir = resolve_ex_dir(slug, owner)
    embedder = Embedder(
        api_key=emb_cfg["api_key"],
        base_url=emb_cfg["base_url"],
        model=emb_cfg["model"],
    )
    store = VectorStore(
        persist_dir=str(ex_dir / "chroma_db"),
        collection_name=get_collection_name(slug),
    )
    # 先清空：旧参数切片与新参数切片混存会污染检索
    store.delete_collection()
    return (
        VectorStore(
            persist_dir=str(ex_dir / "chroma_db"),
            collection_name=get_collection_name(slug),
        ),
        embedder,
    )


def _rebuild_from_file(slug: str, src_path: Path, owner=None) -> str:
    """从源文件重建。仅用于归档缺失的存量镜像。"""
    from config import CHUNK_OVERLAP, CHUNK_TURNS

    vector_store, embedder = _fresh_store(slug, owner)
    ext = src_path.suffix.lower()
    if ext in (".mht", ".mhtml"):
        from memory.ingest import ingest_qq_file

        messages, chunk_count = ingest_qq_file(
            str(src_path), slug, "", vector_store, embedder, owner=owner
        )
    elif ext == ".txt":
        from parsers.wechat_parser import detect_format

        if detect_format(str(src_path)) == "plaintext":
            from memory.ingest import ingest_qq_file

            messages, chunk_count = ingest_qq_file(
                str(src_path), slug, "", vector_store, embedder, owner=owner
            )
        else:
            from memory.ingest import ingest_wechat_file

            messages, chunk_count = ingest_wechat_file(
                str(src_path), slug, "", vector_store, embedder, owner=owner
            )
    else:
        from memory.ingest import ingest_wechat_file

        messages, chunk_count = ingest_wechat_file(
            str(src_path), slug, "", vector_store, embedder, owner=owner
        )
    return (
        f"重建完成：共 {len(messages)} 条消息，按 {CHUNK_TURNS} 轮/"
        f"重叠 {CHUNK_OVERLAP} 入库 {chunk_count} 个切片"
    )


def cmd_vector_rebuild(args: str) -> None:
    """CLI 入口：/rebuild-vectors {slug} [聊天记录文件]"""
    from core.corpus_store import has_corpus

    parts = args.strip().split(None, 1)
    if not parts:
        print("用法: /rebuild-vectors {slug} [聊天记录文件]")
        return

    slug = parts[0]
    try:
        validate_slug(slug)
    except ValueError as e:
        print(f"非法 slug: {e}")
        return

    ex_dir, owner = find_ex_dir_with_owner(slug)
    if ex_dir is None:
        print(f"镜像 [{slug}] 不存在")
        return

    try:
        if len(parts) == 1:
            if not has_corpus(slug, owner):
                print(
                    f"镜像 [{slug}] 没有语料归档（应为改造前导入的存量镜像），"
                    "请提供源文件：/rebuild-vectors {slug} {聊天记录文件}"
                )
                return
            print(_rebuild_from_corpus(slug, owner))
        else:
            src_path = Path(parts[1].strip())
            if not src_path.exists():
                print(f"源文件不存在: {src_path}")
                return
            print(_rebuild_from_file(slug, src_path, owner))
    except RuntimeError as e:
        print(str(e))
    except Exception as e:  # 网络/解析/入库异常不应让 CLI 崩溃
        print(f"重建失败: {e}")
        logger.exception("镜像 [%s] 向量重建失败", slug)


register("rebuild-vectors", cmd_vector_rebuild)
