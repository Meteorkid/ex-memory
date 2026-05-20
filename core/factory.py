"""统一工厂：VectorStore + Embedder + ChatEngine 初始化。"""

from config import get_ex_dir, get_collection_name, get_embedding_config


def create_engine_and_store(slug: str):
    """创建 ChatEngine、VectorStore、Embedder 的统一入口。

    Returns:
        (ChatEngine, VectorStore | None, Embedder | None)
    """
    from core.engine import ChatEngine
    from memory.vector_store import VectorStore
    from memory.embedder import Embedder

    ex_dir = get_ex_dir(slug)
    embedder = None
    vector_store = None

    chroma_dir = ex_dir / "chroma_db"
    if chroma_dir.exists() and any(chroma_dir.iterdir()):
        emb_cfg = get_embedding_config()
        if emb_cfg["api_key"]:
            try:
                embedder = Embedder(
                    api_key=emb_cfg["api_key"],
                    base_url=emb_cfg["base_url"],
                    model=emb_cfg["model"],
                )
                vector_store = VectorStore(
                    persist_dir=str(chroma_dir),
                    collection_name=get_collection_name(slug),
                )
            except Exception as e:
                import logging
                logger = logging.getLogger("ex-memory")
                logger.warning("向量库加载失败: %s，降级为纯文本模式", e)

    engine = ChatEngine(slug=slug, vector_store=vector_store, embedder=embedder)
    return engine, vector_store, embedder
