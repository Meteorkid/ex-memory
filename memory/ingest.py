"""数据摄入工具：解析文件 → 切片 → 入库，消除 run.py 与 routes.py 中的重复逻辑。"""

import json
import logging

from core.privacy import mask_sensitive

logger = logging.getLogger("ex-memory")


def _archive_messages(slug, messages, source, owner, import_id=None):
    if import_id is None:
        from core.corpus_store import append_messages

        append_messages(slug, messages, source=source, owner=owner)
        return

    from core.corpus_store import CORPUS_FILENAME
    from core.mirror_store import mirror_store

    def append_once(current):
        for line in current.splitlines():
            try:
                if json.loads(line).get("_import_id") == import_id:
                    return current
            except (json.JSONDecodeError, AttributeError):
                continue
        records = [
            {**message, "_source": source, "_import_id": import_id}
            for message in messages
        ]
        prefix = current + ("\n" if current and not current.endswith("\n") else "")
        return prefix + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)

    # 与语料追加共用 corpus.jsonl.lock：失败重传不能重复归档或覆盖其他导入。
    mirror_store(slug, owner).locked_update_text(CORPUS_FILENAME, append_once)


def _validate_import_messages(messages, require_target):
    if require_target and not any(
        message.get("is_target") and message.get("content", "").strip()
        for message in messages
    ):
        raise ValueError("未找到目标人物的有效消息，请核对记录中的说话人名称或导出格式")


def _mask_messages(messages: list[dict]) -> list[dict]:
    """导入内容脱敏：手机号/身份证/银行卡/邮箱不以明文入向量库。

    仅处理这四类对语气还原无价值的模式，不扩大脱敏范围。
    """
    for msg in messages:
        content = msg.get("content")
        if content:
            msg["content"] = mask_sensitive(content)
    return messages


def _prepare_account_index(slug, owner, vector_store, embedder):
    if owner is not None:
        from core.factory import check_embedding_identity

        check_embedding_identity(
            slug, owner, vector_store, embedder.model_config, initialize=True
        )


def ingest_wechat_file(
    file_path: str,
    slug: str,
    target_name: str,
    vector_store,
    embedder,
    owner=None,
    *,
    import_id=None,
    require_target=False,
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

    messages = _mask_messages(wechat_parse(file_path, target_name=target_name))
    _validate_import_messages(messages, require_target)
    logger.info("解析完成: %d 条消息", len(messages))

    if not messages:
        return [], 0

    # 归档脱敏后的消息：没有可重放的输入，换模型/调分块/迁移向量库都会卡死
    _archive_messages(slug, messages, "wechat", owner, import_id)

    chunker = Chunker()
    chunks = chunker.chunk_messages(messages, source="wechat", chat_id=f"wechat_{slug}")
    logger.info("切片完成: %d 个 chunks", len(chunks))

    if chunks:
        _prepare_account_index(slug, owner, vector_store, embedder)
        vector_store.ingest(chunks, embedder)
        logger.info("入库完成: %d 条记录", vector_store.count())

    return messages, len(chunks)


def ingest_qq_file(
    file_path: str,
    slug: str,
    target_name: str,
    vector_store,
    embedder,
    owner=None,
    *,
    import_id=None,
    require_target=False,
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

    messages = _mask_messages(qq_parse(file_path, target_name=target_name))
    if require_target:
        # Web 导入使用独立的原始说话人字段，避免 QQ 的昵称子串匹配混入其他人。
        for message in messages:
            message["is_target"] = (
                bool(target_name.strip())
                and message.get("sender", "").strip() == target_name.strip()
            )
    _validate_import_messages(messages, require_target)
    logger.info("QQ 解析完成: %d 条消息", len(messages))

    if not messages:
        return [], 0

    _archive_messages(slug, messages, "qq", owner, import_id)

    chunker = Chunker()
    chunks = chunker.chunk_messages(messages, source="qq", chat_id=f"qq_{slug}")
    logger.info("切片完成: %d 个 chunks", len(chunks))

    if chunks:
        _prepare_account_index(slug, owner, vector_store, embedder)
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
    chunks = chunker.chunk_text(mask_sensitive(text), source=source)
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
