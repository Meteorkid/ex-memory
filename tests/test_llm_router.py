"""LLM 多供应商路由与熔断（FR-038）。

retry.py 解决单次调用的偶发失败，解决不了供应商整体故障——
后者下每个请求都要先重试再失败，延迟与成本都被放大。
"""

from unittest.mock import MagicMock

import pytest

from core.llm_router import (
    AllProvidersFailed,
    LLMRouter,
    ProviderConfig,
)


def _providers(*names):
    return [
        ProviderConfig(name=n, api_key="k", base_url="http://x", model="m", priority=i)
        for i, n in enumerate(names)
    ]


def _err(status):
    e = RuntimeError(f"http {status}")
    e.status_code = status
    return e


@pytest.fixture(autouse=True)
def stub_clients(monkeypatch):
    """路由不真的建 OpenAI 客户端。"""
    monkeypatch.setattr(
        LLMRouter, "_client_for", lambda self, p: MagicMock(name=p.name)
    )


class TestFailover:
    def test_falls_over_to_next_provider(self):
        router = LLMRouter(_providers("a", "b"), failure_threshold=3)
        seen = []

        def invoke(client, model, **kwargs):
            seen.append(model)
            if len(seen) == 1:
                raise _err(503)
            return "ok"

        result, provider = router.call(invoke)
        assert result == "ok"
        assert provider.name == "b"

    def test_permanent_error_does_not_try_other_providers(self):
        """400 这类问题出在请求本身，换供应商是一样的结果。"""
        router = LLMRouter(_providers("a", "b"))
        calls = []

        def invoke(client, model, **kwargs):
            calls.append(model)
            raise _err(400)

        with pytest.raises(RuntimeError):
            router.call(invoke)
        assert len(calls) == 1

    def test_all_failing_raises_all_providers_failed(self):
        router = LLMRouter(_providers("a", "b"))

        def invoke(client, model, **kwargs):
            raise _err(503)

        with pytest.raises(AllProvidersFailed):
            router.call(invoke)

    def test_single_provider_still_works(self):
        """未配置多供应商时行为与改造前一致。"""
        router = LLMRouter(_providers("only"))
        result, provider = router.call(lambda c, m, **k: "fine")
        assert result == "fine" and provider.name == "only"


class TestCircuitBreaker:
    def test_opens_after_threshold_and_skips_provider(self):
        router = LLMRouter(_providers("a", "b"), failure_threshold=2)
        attempted = []

        def failing_a(client, model, **kwargs):
            attempted.append(model)
            raise _err(503)

        # 两次失败把 a 熔断
        for _ in range(2):
            with pytest.raises(AllProvidersFailed):
                router.call(failing_a)

        assert [s["open"] for s in router.status() if s["name"] == "a"] == [True]
        assert [p.name for p in router.available()] == []

    def test_cooldown_expiry_reopens_provider(self):
        router = LLMRouter(_providers("a"), failure_threshold=1, cooldown_seconds=0.05)

        def failing(client, model, **kwargs):
            raise _err(503)

        with pytest.raises(AllProvidersFailed):
            router.call(failing)
        assert router.available() == []

        import time

        time.sleep(0.08)
        # 冷却期满：半开，放探测请求进去
        assert [p.name for p in router.available()] == ["a"]

    def test_success_resets_failure_count(self):
        router = LLMRouter(_providers("a"), failure_threshold=3)
        calls = []

        def flaky(client, model, **kwargs):
            calls.append(1)
            if len(calls) < 2:
                raise _err(503)
            return "ok"

        with pytest.raises(AllProvidersFailed):
            router.call(flaky)
        router.call(flaky)
        assert router.status()[0]["consecutive_failures"] == 0

    def test_all_open_still_attempts_primary(self):
        """全部熔断时宁可失败也好过直接拒绝服务——万一已经恢复了呢。"""
        router = LLMRouter(_providers("a"), failure_threshold=1)

        def failing(client, model, **kwargs):
            raise _err(503)

        with pytest.raises(AllProvidersFailed):
            router.call(failing)

        attempted = []

        def recovered(client, model, **kwargs):
            attempted.append(model)
            return "back"

        result, _ = router.call(recovered)
        assert result == "back" and attempted


class TestConfigParsing:
    def test_empty_config_yields_single_shared_client_provider(self, monkeypatch):
        from core import llm_router

        monkeypatch.setattr("config.LLM_PROVIDERS", "")
        providers = llm_router._parse_providers()
        assert len(providers) == 1
        assert providers[0].use_shared_client is True

    def test_json_config_is_parsed_in_order(self, monkeypatch):
        import json

        from core import llm_router

        monkeypatch.setattr(
            "config.LLM_PROVIDERS",
            json.dumps(
                [
                    {"name": "primary", "model": "m1"},
                    {"name": "backup", "model": "m2"},
                ]
            ),
        )
        providers = llm_router._parse_providers()
        assert [p.name for p in providers] == ["primary", "backup"]
        assert providers[0].use_shared_client is False

    def test_invalid_json_is_rejected_loudly(self, monkeypatch):
        from core import llm_router

        monkeypatch.setattr("config.LLM_PROVIDERS", "{不是合法 JSON")
        with pytest.raises(ValueError, match="JSON"):
            llm_router._parse_providers()

    def test_empty_provider_list_is_rejected(self):
        with pytest.raises(ValueError):
            LLMRouter([])
