"""语料归档：把解析脱敏后的消息落盘。

改造前导入是「解析 → 切片 → 向量化 → 丢弃原始消息」，源文件也在请求结束
时删掉。后果是**向量无法重建**：换 embedding 模型、调分块参数、迁移向量库
都会卡死，因为没有可重放的输入。PRD §14.3 的「按镜像重建向量」策略在这个
前提下根本执行不了。

存的是**脱敏后**的消息（手机号/身份证/银行卡/邮箱已掩码），不是原始上传
文件。这些文本本来就以切片形式存在向量库里，归档不引入新的暴露面，但让
重建成为可能。

归档同样受留存策略约束：账号注销时随镜像目录一起删除。
"""

import json
import logging
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger("ex-memory")

CORPUS_FILENAME = "corpus.jsonl"


def corpus_path(slug: str, owner: Optional[int] = None) -> Path:
    import config

    return config.resolve_ex_dir(slug, owner) / CORPUS_FILENAME


def _store(slug: str, owner: Optional[int] = None):
    from core.mirror_store import mirror_store

    return mirror_store(slug, owner)


def append_messages(
    slug: str, messages: list[dict], source: str, owner: Optional[int] = None
) -> int:
    """追加一批消息到语料归档，返回写入条数。

    追加而非覆盖：一个镜像可以多次导入不同文件，重建时要能重放全部。
    """
    if not messages:
        return 0
    records = [{**message, "_source": source} for message in messages]
    written = _store(slug, owner).append_jsonl(CORPUS_FILENAME, records)
    logger.info("语料归档写入 %d 条 slug=%s source=%s", written, slug, source)
    return written


def iter_messages(slug: str, owner: Optional[int] = None) -> Iterator[dict]:
    """按写入顺序重放归档消息。损坏行跳过并告警。"""
    store = _store(slug, owner)
    if not store.exists(CORPUS_FILENAME):
        return
    for line_no, line in enumerate(store.read_text(CORPUS_FILENAME).splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.warning("语料归档第 %d 行损坏，已跳过 slug=%s", line_no, slug)


def load_messages(slug: str, owner: Optional[int] = None) -> list[dict]:
    return list(iter_messages(slug, owner))


def count_messages(slug: str, owner: Optional[int] = None) -> int:
    return sum(1 for _ in iter_messages(slug, owner))


def has_corpus(slug: str, owner: Optional[int] = None) -> bool:
    return _store(slug, owner).exists(CORPUS_FILENAME)
