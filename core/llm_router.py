"""LLM 多供应商路由与熔断。

retry.py 解决的是「单次调用偶发失败」，解决不了「供应商整体故障」——
后者下每个请求都要先重试三次再失败，延迟和成本都被放大，用户还是拿不到
回复。这里加两层：

1. **熔断**：连续失败到阈值就把该供应商标记为不可用，在冷却期内直接跳过，
   不再浪费重试。冷却期满放一个探测请求进去，成功即恢复。
2. **降级路由**：主供应商熔断时按序切到备用供应商。

供应商配置来自 config.LLM_PROVIDERS（JSON），为空时退化为单供应商，
行为与改造前一致。
"""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("ex-memory")


@dataclass
class ProviderConfig:
    name: str
    api_key: str
    base_url: str
    model: str
    # 权重仅用于日志与后续的成本归集，当前按顺序故障转移
    priority: int = 0
    # 单供应商（未配置 LLM_PROVIDERS）时复用 config 里那个共享客户端，
    # 避免同一份配置维护两个连接池
    use_shared_client: bool = False


@dataclass
class _Breaker:
    """单个供应商的熔断状态。"""

    failure_threshold: int
    cooldown_seconds: float
    consecutive_failures: int = 0
    opened_at: Optional[float] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def is_open(self) -> bool:
        with self.lock:
            if self.opened_at is None:
                return False
            if time.monotonic() - self.opened_at >= self.cooldown_seconds:
                # 冷却期满：半开，放一个探测请求进去
                self.opened_at = None
                self.consecutive_failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self.lock:
            self.consecutive_failures = 0
            self.opened_at = None

    def record_failure(self) -> bool:
        """记录失败，返回是否因此熔断。"""
        with self.lock:
            self.consecutive_failures += 1
            if (
                self.consecutive_failures >= self.failure_threshold
                and self.opened_at is None
            ):
                self.opened_at = time.monotonic()
                return True
            return False


class AllProvidersFailed(RuntimeError):
    """所有供应商都不可用。"""


class LLMRouter:
    def __init__(
        self,
        providers: list[ProviderConfig],
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ):
        if not providers:
            raise ValueError("至少需要一个 LLM 供应商")
        self._providers = providers
        self._breakers = {
            p.name: _Breaker(failure_threshold, cooldown_seconds) for p in providers
        }
        self._clients: dict[str, Any] = {}
        self._client_lock = threading.Lock()

    def _client_for(self, provider: ProviderConfig) -> Any:
        if provider.use_shared_client:
            from config import get_llm_client

            return get_llm_client()
        with self._client_lock:
            client = self._clients.get(provider.name)
            if client is None:
                from openai import OpenAI

                client = OpenAI(
                    api_key=provider.api_key, base_url=provider.base_url, timeout=60.0
                )
                self._clients[provider.name] = client
            return client

    def available(self) -> list[ProviderConfig]:
        return [p for p in self._providers if not self._breakers[p.name].is_open()]

    def call(self, invoke, **kwargs) -> tuple[Any, ProviderConfig]:
        """按序尝试可用供应商。

        invoke(client, model, **kwargs) 由调用方提供，路由层不关心
        具体调的是 chat 还是 stream。返回 (结果, 实际使用的供应商)。
        """
        from core.retry import is_retryable

        candidates = self.available()
        if not candidates:
            # 全部熔断时仍试一次主供应商：宁可失败也好过直接拒绝服务，
            # 万一供应商已经恢复了呢
            logger.error("全部 LLM 供应商处于熔断状态，强制尝试主供应商")
            candidates = self._providers[:1]

        last_error: Optional[Exception] = None
        for provider in candidates:
            breaker = self._breakers[provider.name]
            try:
                result = invoke(self._client_for(provider), provider.model, **kwargs)
                breaker.record_success()
                return result, provider
            except Exception as e:  # noqa: BLE001 — 要按错误类型决定是否换供应商
                last_error = e
                if not is_retryable(e):
                    # 请求本身有问题（400/422 之类），换供应商也是一样的结果
                    logger.warning(
                        "供应商 %s 返回不可重试的错误，不再尝试其他供应商: %s",
                        provider.name,
                        e,
                    )
                    raise
                if breaker.record_failure():
                    logger.error("供应商 %s 连续失败达到阈值，已熔断", provider.name)
                logger.warning("供应商 %s 调用失败，尝试下一个: %s", provider.name, e)

        raise AllProvidersFailed(
            f"所有 LLM 供应商均不可用，最后错误: {last_error}"
        ) from last_error

    def status(self) -> list[dict]:
        """供观测用：各供应商的熔断状态。"""
        return [
            {
                "name": p.name,
                "model": p.model,
                "open": self._breakers[p.name].is_open(),
                "consecutive_failures": self._breakers[p.name].consecutive_failures,
            }
            for p in self._providers
        ]


_router: Optional[LLMRouter] = None
_router_lock = threading.Lock()


def _parse_providers() -> list[ProviderConfig]:
    """从配置构造供应商列表。

    LLM_PROVIDERS 为空时退化为单供应商（用既有的 LLM_* 配置），
    行为与改造前完全一致。
    """
    import config

    raw = (config.LLM_PROVIDERS or "").strip()
    if not raw:
        return [
            ProviderConfig(
                name="default",
                api_key=config.LLM_API_KEY,
                base_url=config.LLM_BASE_URL,
                model=config.LLM_MODEL,
                use_shared_client=True,
            )
        ]
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM_PROVIDERS 不是合法 JSON: {e}") from e

    providers = []
    for index, entry in enumerate(entries):
        providers.append(
            ProviderConfig(
                name=entry.get("name") or f"provider{index}",
                api_key=entry.get("api_key") or config.LLM_API_KEY,
                base_url=entry.get("base_url") or config.LLM_BASE_URL,
                model=entry.get("model") or config.LLM_MODEL,
                priority=index,
            )
        )
    if not providers:
        raise ValueError("LLM_PROVIDERS 为空数组")
    return providers


def get_router() -> LLMRouter:
    global _router
    with _router_lock:
        if _router is None:
            import config

            _router = LLMRouter(
                _parse_providers(),
                failure_threshold=config.LLM_BREAKER_THRESHOLD,
                cooldown_seconds=config.LLM_BREAKER_COOLDOWN_SECONDS,
            )
        return _router


def reset_for_tests() -> None:
    global _router
    with _router_lock:
        _router = None
