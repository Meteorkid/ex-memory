"""聊天记录导入编排：上传文件路径 -> 解析 -> 向量入库。"""

from dataclasses import dataclass
from pathlib import Path

import config


class MissingEmbeddingConfig(RuntimeError):
    """Embedding 配置缺失，无法进行向量入库。"""


@dataclass(frozen=True)
class ImportResult:
    messages_count: int
    chunk_count: int


def import_chat_file(file_path: Path, slug: str, target_name: str) -> ImportResult:
    """导入聊天记录文件，并返回解析消息数与入库切片数。"""
    emb_cfg = config.get_embedding_config()
    if not emb_cfg["api_key"]:
        raise MissingEmbeddingConfig("未配置 Embedding API Key")

    from memory.embedder import Embedder
    from memory.vector_store import VectorStore

    ex_dir = config.get_ex_dir(slug)
    embedder = Embedder(
        api_key=emb_cfg["api_key"],
        base_url=emb_cfg["base_url"],
        model=emb_cfg["model"],
    )
    vector_store = VectorStore(
        persist_dir=str(ex_dir / "chroma_db"),
        collection_name=config.get_collection_name(slug),
    )

    messages, chunk_count = _ingest_by_extension(
        file_path=file_path,
        slug=slug,
        target_name=target_name,
        vector_store=vector_store,
        embedder=embedder,
    )
    return ImportResult(messages_count=len(messages), chunk_count=chunk_count)


def _ingest_by_extension(file_path: Path, slug: str, target_name: str, vector_store, embedder):
    """根据文件扩展名选择微信/QQ 摄入器。"""
    ext = file_path.suffix.lower()
    if ext in (".mht", ".mhtml"):
        from memory.ingest import ingest_qq_file
        return ingest_qq_file(str(file_path), slug, target_name, vector_store, embedder)

    if ext == ".txt":
        from parsers.wechat_parser import detect_format
        fmt = detect_format(str(file_path))
        if fmt == "plaintext":
            from memory.ingest import ingest_qq_file
            return ingest_qq_file(str(file_path), slug, target_name, vector_store, embedder)

    from memory.ingest import ingest_wechat_file
    return ingest_wechat_file(str(file_path), slug, target_name, vector_store, embedder)
