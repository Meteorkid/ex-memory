"""Embedding API 封装：支持 OpenAI 兼容的 embedding 端点。"""

from openai import OpenAI


class Embedder:
    def __init__(self, api_key: str, base_url: str, model: str = "BAAI/bge-m3"):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding。"""
        if not texts:
            return []
        response = self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in response.data]

    def embed_one(self, text: str) -> list[float]:
        """单条 embedding。"""
        response = self.client.embeddings.create(input=[text], model=self.model)
        return response.data[0].embedding
