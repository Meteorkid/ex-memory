"""Embedding API 封装：支持 OpenAI 兼容的 embedding 端点，含重试与批处理。"""

import logging
from openai import OpenAI, APIError, APITimeoutError, APIConnectionError
from core.retry import retry_api
from core.model_client import ModelProviderError, provider_call

logger = logging.getLogger("ex-memory")

MAX_BATCH_SIZE = 100  # 单次 API 调用最大文本数


class EmbeddingError(Exception):
    """Embedding 服务不可用异常。"""

    pass


class Embedder:
    def __init__(self, api_key: str, base_url: str, model: str = "BAAI/bge-m3", *, owner=None, model_config=None):
        self.owner = owner
        self.model_config = model_config
        kwargs = {"api_key": api_key, "base_url": base_url, "timeout": 30.0}
        if owner is not None:
            from core.provider_transport import build_provider_http_client

            kwargs["http_client"] = build_provider_http_client(base_url)
        self.client = provider_call(OpenAI, **kwargs) if owner is not None else OpenAI(**kwargs)
        self.model = model

    @retry_api(max_attempts=3, base_delay=1.0)
    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """单次 API 调用（含指数退避重试）。"""
        if self.owner is not None:
            response = provider_call(self.client.embeddings.create, input=texts, model=self.model)
        else:
            response = self.client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in response.data]

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding，自动分批超过 MAX_BATCH_SIZE 的请求。"""
        if not texts:
            return []

        if len(texts) <= MAX_BATCH_SIZE:
            return self._call_api(texts)

        # 分批处理
        all_embeddings = []
        for i in range(0, len(texts), MAX_BATCH_SIZE):
            batch = texts[i : i + MAX_BATCH_SIZE]
            try:
                all_embeddings.extend(self._call_api(batch))
            except (APIError, APITimeoutError, APIConnectionError, ModelProviderError) as e:
                logger.error(
                    "Embedding 批次 %d/%d 失败: %s",
                    i // MAX_BATCH_SIZE + 1,
                    (len(texts) + MAX_BATCH_SIZE - 1) // MAX_BATCH_SIZE,
                    e,
                )
                raise EmbeddingError(f"Embedding 服务调用失败: {e}") from e
        return all_embeddings

    def embed_one(self, text: str) -> list[float]:
        """单条 embedding。"""
        try:
            result = self._call_api([text])
            return result[0]
        except (APIError, APITimeoutError, APIConnectionError, ModelProviderError) as e:
            logger.error("Embedding 单条调用失败: %s", e)
            raise EmbeddingError(f"Embedding 服务调用失败: {e}") from e
