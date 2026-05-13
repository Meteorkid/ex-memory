"""数据摄入工具：解析文件 → 切片 → 入库，消除 run.py 与 routes.py 中的重复逻辑。"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ex-memory")


def ingest_wechat_file(
    file_path: str,
    slug: str,
    target_name: str,
    vector_store,
    embedder,
) -> tuple[list[dict], int]:
    """解析微信聊天记录并入库。

    Args:
        file_path: 聊天记录文件路径
        slug: 镜像 slug
        target_name: 目标对象名称
        vector_store: VectorStore 实例
        embedder: Embedder 实例

    Returns:
        (messages, chunk_count) 元组
    """
    from parsers.wechat_parser import parse as wechat_parse
    from memory.chunker import Chunker

    messages = wechat_parse(file_path, target_name=target_name)
    logger.info("解析完成: %d 条消息", len(messages))

    if not messages:
        return [], 0

    chunker = Chunker()
    chunks = chunker.chunk_messages(messages, source="wechat", chat_id=f"wechat_{slug}")
    logger.info("切片完成: %d 个 chunks", len(chunks))

    if chunks:
        vector_store.ingest(chunks, embedder)
        logger.info("入库完成: %d 条记录", vector_store.count())

    return messages, len(chunks)


def ingest_qq_file(
    file_path: str,
    slug: str,
    target_name: str,
    vector_store,
    embedder,
) -> tuple[list[dict], int]:
    """解析 QQ 聊天记录并入库。

    Args:
        file_path: 聊天记录文件路径
        slug: 镜像 slug
        target_name: 目标对象名称
        vector_store: VectorStore 实例
        embedder: Embedder 实例

    Returns:
        (messages, chunk_count) 元组
    """
    from parsers.qq_parser import parse as qq_parse
    from memory.chunker import Chunker

    messages = qq_parse(file_path, target_name=target_name)
    logger.info("QQ 解析完成: %d 条消息", len(messages))

    if not messages:
        return [], 0

    chunker = Chunker()
    chunks = chunker.chunk_messages(messages, source="qq", chat_id=f"qq_{slug}")
    logger.info("切片完成: %d 个 chunks", len(chunks))

    if chunks:
        vector_store.ingest(chunks, embedder)
        logger.info("入库完成: %d 条记录", vector_store.count())

    return messages, len(chunks)


def ingest_text(
    text: str,
    slug: str,
    source: str,
    vector_store,
    embedder,
) -> int:
    """切片并入库文本内容。

    Returns:
        入库的 chunk 数量
    """
    from memory.chunker import Chunker

    chunker = Chunker()
    chunks = chunker.chunk_text(text, source=source)
    if chunks:
        vector_store.ingest(chunks, embedder)
    return len(chunks)


def build_materials_summary(
    vector_store,
    embedder,
    messages_count: int = 0,
    prefix: str = "",
) -> str:
    """从向量库采样构建材料摘要。

    Returns:
        材料摘要文本
    """
    summary = prefix
    sample_results = vector_store.search("日常对话", embedder, top_k=20)
    if sample_results:
        if prefix:
            summary += "\n"
        summary += "## 聊天记录样本\n"
        for r in sample_results[:10]:
            summary += f"- {r.get('display_text', '')[:200]}\n"
    return summary
