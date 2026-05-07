"""ChromaDB 向量库封装：ingest / search / search_target_only。"""

from typing import Optional
import chromadb
from memory.embedder import Embedder


class VectorStore:
    def __init__(self, persist_dir: str, collection_name: str):
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def ingest(self, chunks: list[dict], embedder: Embedder, batch_size: int = 100):
        """批量写入 chunks。

        每个 chunk 应包含:
            - id: 唯一标识
            - text_for_embedding: 用于向量化的文本
            - display_text: 用于展示的原文
            - metadata: source, chat_id, dominant_speaker, start_ts, end_ts, ...
        """
        total = len(chunks)
        for i in range(0, total, batch_size):
            batch = chunks[i : i + batch_size]
            ids = [c["id"] for c in batch]
            documents = [c["text_for_embedding"] for c in batch]
            metadatas = []
            for c in batch:
                meta = {k: v for k, v in c.get("metadata", {}).items() if v is not None}
                # ChromaDB metadata 值只能是 str/int/float/bool
                for k, v in meta.items():
                    if isinstance(v, list):
                        meta[k] = str(v)
                meta["display_text"] = c.get("display_text", "")
                metadatas.append(meta)

            embeddings = embedder.embed(documents)

            self.collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
            print(f"  入库进度: {min(i + batch_size, total)}/{total}")

    def search(
        self,
        query: str,
        embedder: Embedder,
        top_k: int = 10,
        where: Optional[dict] = None,
        where_document: Optional[dict] = None,
    ) -> list[dict]:
        """语义检索，支持 metadata 过滤。"""
        query_embedding = embedder.embed_one(query)
        kwargs = {
            "query_embeddings": [query_embedding],
            "n_results": top_k,
        }
        if where:
            kwargs["where"] = where
        if where_document:
            kwargs["where_document"] = where_document

        results = self.collection.query(**kwargs)

        output = []
        if results and results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                score = 1 - results["distances"][0][i] if results["distances"] else 0
                output.append({
                    "display_text": meta.get("display_text", doc),
                    "score": score,
                    "metadata": meta,
                })
        return output

    def search_target_only(
        self, query: str, embedder: Embedder, top_k: int = 10
    ) -> list[dict]:
        """便捷方法：只检索 dominant_speaker == 'target' 的原话。"""
        return self.search(
            query=query,
            embedder=embedder,
            top_k=top_k,
            where={"dominant_speaker": "target"},
        )

    def add_session_summary(self, text: str, slug: str, embedder: Embedder):
        """将 session 摘要写入向量库。"""
        embedding = embedder.embed_one(text)
        self.collection.add(
            ids=[f"session_{slug}_{hash(text)}"],
            embeddings=[embedding],
            documents=[text],
            metadatas=[{
                "source": "session_summary",
                "dominant_speaker": "session",
                "display_text": text[:200],
            }],
        )

    def count(self) -> int:
        """返回 collection 中的记录数。"""
        return self.collection.count()

    def delete_collection(self):
        """删除整个 collection。"""
        self.client.delete_collection(self.collection.name)
