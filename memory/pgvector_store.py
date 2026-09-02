"""pgvector 向量库：与 ChromaDB 版 VectorStore 同签名。

换掉 ChromaDB 的真实理由不是性能，是**本地盘**：Chroma 的 persist 目录在
每个副本各自的磁盘上，多副本下共享不了。向量进 Postgres 之后副本才真正
无状态。顺带解决一致性——向量与镜像元数据同库，账号注销时一起删，不会留下
孤儿向量（那是合规事故而不只是脏数据）。

接口刻意与 VectorStore 完全一致，调用方（engine / ingest / rebuild）不用改。
"""

import hashlib
import json
import logging
from typing import Callable, Optional

from memory.embedder import Embedder
from memory.vector_store import IngestError

logger = logging.getLogger("ex-memory")


class PgVectorStore:
    def __init__(self, persist_dir: str, collection_name: str):
        # persist_dir 保留在签名里只为与 ChromaDB 版兼容，pgvector 用不到
        self.exe_key = collection_name
        self._deleted = False

    @staticmethod
    def _vector_literal(values: list[float]) -> str:
        return "[" + ",".join(repr(float(v)) for v in values) + "]"

    def _conn(self):
        from server.auth import _get_conn

        return _get_conn()

    def ingest(
        self,
        chunks: list[dict],
        embedder: Embedder,
        batch_size: int = 100,
        on_batch: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        total = len(chunks)
        total_batches = (total + batch_size - 1) // batch_size
        batches_done = 0

        for start in range(0, total, batch_size):
            batch = chunks[start : start + batch_size]
            documents = [c["text_for_embedding"] for c in batch]
            try:
                embeddings = embedder.embed(documents)
            except Exception as e:
                raise IngestError(
                    f"Embedding 失败 (批次 {batches_done + 1}/{total_batches}): {e}",
                    batches_completed=batches_done,
                ) from e

            try:
                with self._conn() as conn:
                    for chunk, embedding in zip(batch, embeddings):
                        metadata = {
                            k: v
                            for k, v in chunk.get("metadata", {}).items()
                            if v is not None
                        }
                        conn.execute(
                            """
                            INSERT INTO memory_vectors
                                (id, exe_key, embedding, document, display_text, metadata)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT (id) DO UPDATE SET
                                embedding = EXCLUDED.embedding,
                                document = EXCLUDED.document,
                                display_text = EXCLUDED.display_text,
                                metadata = EXCLUDED.metadata
                            """,
                            (
                                chunk["id"],
                                self.exe_key,
                                self._vector_literal(embedding),
                                chunk["text_for_embedding"],
                                chunk.get("display_text", ""),
                                json.dumps(metadata, ensure_ascii=False),
                            ),
                        )
                    conn.commit()
            except Exception as e:
                raise IngestError(
                    f"pgvector 写入失败 (批次 {batches_done + 1}/{total_batches}): {e}",
                    batches_completed=batches_done,
                ) from e

            batches_done += 1
            logger.info("入库进度: %d/%d", min(start + batch_size, total), total)
            if on_batch:
                on_batch(batches_done, total_batches)

    def search(
        self,
        query: str,
        embedder: Embedder,
        top_k: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
    ) -> list[dict]:
        """语义检索。score 口径与 ChromaDB 版一致：1 - 余弦距离。"""
        embedding = self._vector_literal(embedder.embed_one(query))
        conditions = ["exe_key = ?"]
        params: list = [self.exe_key]
        for key, value in (where or {}).items():
            conditions.append("metadata->>? = ?")
            params.extend([key, str(value)])

        sql = f"""
            SELECT display_text, document, metadata,
                   1 - (embedding <=> ?::vector) AS score
            FROM memory_vectors
            WHERE {" AND ".join(conditions)}
            ORDER BY embedding <=> ?::vector
            LIMIT ?
        """
        with self._conn() as conn:
            rows = conn.execute(sql, (embedding, *params, embedding, top_k)).fetchall()

        output = []
        for row in rows:
            metadata = row["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            output.append(
                {
                    "display_text": metadata.get("display_text")
                    or row["display_text"]
                    or row["document"],
                    "score": float(row["score"]),
                    "metadata": metadata,
                }
            )
        return output

    def search_target_only(
        self, query: str, embedder: Embedder, top_k: int = 10
    ) -> list[dict]:
        return self.search(
            query=query,
            embedder=embedder,
            top_k=top_k,
            where={"dominant_speaker": "target"},
        )

    def add_session_summary(self, text: str, slug: str, embedder: Embedder) -> None:
        embedding = self._vector_literal(embedder.embed_one(text))
        stable_id = hashlib.md5(text.encode()).hexdigest()[:16]
        metadata = {
            "source": "session_summary",
            "dominant_speaker": "session",
            "display_text": text[:200],
        }
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO memory_vectors
                    (id, exe_key, embedding, document, display_text, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET embedding = EXCLUDED.embedding
                """,
                (
                    f"session_{slug}_{stable_id}",
                    self.exe_key,
                    embedding,
                    text,
                    text[:200],
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            conn.commit()

    def count(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM memory_vectors WHERE exe_key = ?",
                (self.exe_key,),
            ).fetchone()
        return int(row["n"])

    def delete_collection(self) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM memory_vectors WHERE exe_key = ?", (self.exe_key,)
            )
            conn.commit()
        self._deleted = True
