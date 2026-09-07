"""统一工厂：VectorStore + Embedder + ChatEngine 初始化。"""

from typing import Optional

from config import get_collection_name, get_embedding_config


def create_engine_and_store(slug: str, owner: Optional[int] = None):
    """创建 ChatEngine、VectorStore、Embedder 的统一入口。

    owner: 多用户模式下镜像归属账号，用于把各类文件定位到按账号隔离的目录。

    Returns:
        (ChatEngine, VectorStore | None, Embedder | None)
    """
    from core.engine import ChatEngine
    from memory.embedder import Embedder

    embedder = None
    vector_store = None

    emb_cfg = get_embedding_config()
    if emb_cfg["api_key"]:
        try:
            embedder = Embedder(
                api_key=emb_cfg["api_key"],
                base_url=emb_cfg["base_url"],
                model=emb_cfg["model"],
            )
            vector_store = build_vector_store(slug, owner)
            # pgvector 没有「目录存不存在」这种判断，统一用有没有数据来定：
            # 空库等价于没有向量库，降级为纯文本模式
            if vector_store.count() == 0:
                vector_store = None
        except Exception as e:
            import logging

            logger = logging.getLogger("ex-memory")
            logger.warning("向量库加载失败: %s，降级为纯文本模式", e)
            vector_store = None

    engine = ChatEngine(slug=slug, vector_store=vector_store, embedder=embedder)
    return engine, vector_store, embedder


def build_vector_store(slug: str, owner=None):
    """按配置选择向量库后端。

    配了 Postgres 就用 pgvector，否则用 ChromaDB 本地目录。两者接口一致，
    调用方（engine / ingest / rebuild）无需区分。
    """
    import config

    collection = get_collection_name(slug)
    if config.VECTOR_BACKEND == "pgvector":
        from memory.pgvector_store import PgVectorStore

        return PgVectorStore(persist_dir="", collection_name=collection)

    from memory.vector_store import VectorStore

    # chroma_db 是目录树，属于 path() 硬点；将来切对象存储时改用后端专用落位
    from core.mirror_store import mirror_store

    return VectorStore(
        persist_dir=str(mirror_store(slug, owner).path("chroma_db")),
        collection_name=collection,
    )
