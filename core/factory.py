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

    llm_cfg = None
    if owner is not None:
        from config import get_llm_config
        from server.model_settings import MissingModelKey, SettingsConflict

        llm_cfg = get_llm_config(owner)
        try:
            emb_cfg = get_embedding_config(owner)
        except MissingModelKey:
            # 无向量的纯文本镜像仍可使用自己的对话模型。
            vector_store = build_vector_store(slug, owner)
            if vector_store.count():
                raise
            emb_cfg = {"api_key": ""}
            vector_store = None
        if emb_cfg["api_key"] and emb_cfg["revision"] != llm_cfg["revision"]:
            raise SettingsConflict("模型配置已更新，请重试")
    else:
        emb_cfg = get_embedding_config()
    if emb_cfg["api_key"]:
        try:
            embedder = Embedder(
                api_key=emb_cfg["api_key"],
                base_url=emb_cfg["base_url"],
                model=emb_cfg["model"],
                **({"owner": owner, "model_config": emb_cfg} if owner is not None else {}),
            )
            vector_store = build_vector_store(slug, owner)
            if owner is not None:
                check_embedding_identity(slug, owner, vector_store, emb_cfg)
            # pgvector 没有「目录存不存在」这种判断，统一用有没有数据来定：
            # 空库等价于没有向量库，降级为纯文本模式
            if vector_store.count() == 0:
                vector_store = None
        except Exception as e:
            if owner is not None:
                raise
            import logging

            logger = logging.getLogger("ex-memory")
            logger.warning("向量库加载失败: %s，降级为纯文本模式", e)
            vector_store = None

    engine = ChatEngine(
        slug=slug, vector_store=vector_store, embedder=embedder, owner=owner,
        **({"model_config": llm_cfg} if owner is not None else {}),
    )
    return engine, vector_store, embedder


def check_embedding_identity(slug, owner, vector_store, model_config, *, initialize=False):
    """旧向量没有可核验模型标记时拒绝混用；空库只在有效素材入库前锁定。"""
    from core.mirror_store import mirror_store
    from server.model_settings import EmbeddingModelLocked, lock_embedding_model

    store = mirror_store(slug, owner)
    meta = store.read_json("meta.json") if store.exists("meta.json") else {}
    identity = {key: model_config[key] for key in ("provider", "model", "base_url")}
    existing = meta.get("embedding_identity")
    if vector_store.count() and (
        not model_config.get("locked") or existing != identity
    ):
        raise EmbeddingModelLocked("旧向量索引的模型身份无法确认，请重建镜像后再导入")
    if existing is not None and existing != identity:
        raise EmbeddingModelLocked("当前模型与镜像向量索引不一致，请重建镜像后再导入")
    if initialize:
        lock_embedding_model(owner, model_config["revision"])
        def save_identity(current):
            current["embedding_identity"] = identity
            return current
        store.locked_update_json("meta.json", {}, save_identity)
        model_config["locked"] = True


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
