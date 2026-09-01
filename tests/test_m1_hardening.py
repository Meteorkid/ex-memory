"""M1 正确性修复：有界缓存、总上下文预算、重试策略（D-06/07/08/09）。"""

import time
from unittest.mock import MagicMock

import pytest

from core.bounded_cache import BoundedCache


class TestBoundedCache:
    def test_evicts_least_recently_used(self):
        """回归 D-06：原为无上限裸 dict，长期运行必然 OOM。"""
        cache = BoundedCache(maxsize=2)
        cache["a"], cache["b"] = 1, 2
        cache.get("a")  # a 变成最近使用
        cache["c"] = 3
        assert cache.get("b") is None
        assert cache.get("a") == 1 and cache.get("c") == 3

    def test_ttl_expires_entries(self):
        cache = BoundedCache(maxsize=10, ttl_seconds=0.05)
        cache["k"] = "v"
        assert cache.get("k") == "v"
        time.sleep(0.08)
        assert cache.get("k") is None

    def test_on_evict_releases_resources(self):
        released = []
        cache = BoundedCache(maxsize=1, on_evict=lambda k, v: released.append(k))
        cache["a"], cache["b"] = 1, 2
        assert released == ["a"]

    def test_failing_evict_callback_does_not_break_cache(self):
        """释放失败不应污染缓存本身。"""

        def boom(key, value):
            raise RuntimeError("释放失败")

        cache = BoundedCache(maxsize=1, on_evict=boom)
        cache["a"] = 1
        cache["b"] = 2
        assert cache.get("b") == 2

    def test_evict_where_supports_batch_invalidation(self):
        cache = BoundedCache(maxsize=10)
        for uid in (1, 2):
            cache[(uid, "xiaoyu")] = object()
            cache[(uid, "other")] = object()
        assert cache.evict_where(lambda k: k[1] == "xiaoyu") == 2
        assert sorted(k[1] for k in cache.keys()) == ["other", "other"]

    def test_get_or_create_is_single_flight_under_lock(self):
        cache = BoundedCache(maxsize=4)
        calls = []

        def factory():
            calls.append(1)
            return object()

        first = cache.get_or_create("k", factory)
        second = cache.get_or_create("k", factory)
        assert first is second
        assert len(calls) == 1

    def test_maxsize_must_be_positive(self):
        with pytest.raises(ValueError):
            BoundedCache(maxsize=0)


class TestContextBudget:
    """D-08：原先 history 上限约 26 万 tokens，远超模型上下文。"""

    @staticmethod
    def _fit(system, user, history):
        from core.engine import ChatEngine

        return ChatEngine._fit_history_to_budget(system, user, history)

    def test_short_history_is_kept_intact(self):
        history = [{"role": "user", "content": "你好"}] * 4
        assert len(self._fit("人格", "在吗", history)) == 4

    def test_oversized_history_is_trimmed_from_the_oldest_end(self, monkeypatch):
        monkeypatch.setattr("core.engine.LLM_TOTAL_TOKEN_BUDGET", 200)
        history = [
            {"role": "user", "content": f"第{i}条" + "啊" * 100} for i in range(10)
        ]
        kept = self._fit("人格", "在吗", history)
        assert 0 < len(kept) < 10
        # 保留的必须是最近的几条：近处上下文对连贯性更重要
        assert kept[-1]["content"].startswith("第9条")

    def test_history_dropped_entirely_when_persona_fills_budget(self, monkeypatch):
        monkeypatch.setattr("core.engine.LLM_TOTAL_TOKEN_BUDGET", 10)
        history = [{"role": "user", "content": "很长的历史" * 50}]
        assert self._fit("巨大的人格" * 100, "在吗", history) == []

    def test_result_is_ordered_oldest_first(self, monkeypatch):
        monkeypatch.setattr("core.engine.LLM_TOTAL_TOKEN_BUDGET", 400)
        history = [{"role": "user", "content": f"m{i}" + "啊" * 30} for i in range(8)]
        kept = self._fit("人格", "在吗", history)
        indexes = [int(m["content"][1]) for m in kept]
        assert indexes == sorted(indexes)


def _http_error(status, retry_after=None):
    err = RuntimeError("boom")
    err.status_code = status
    if retry_after is not None:
        response = MagicMock()
        response.headers = {"retry-after": str(retry_after)}
        err.response = response
    return err


class TestRetryClassification:
    """D-09：原实现对 400/401 这类永久性错误也重试三次。"""

    def test_permanent_errors_are_not_retried(self):
        from core.retry import retry_api

        calls = []

        @retry_api(max_attempts=3, base_delay=0.001)
        def always_bad_request():
            calls.append(1)
            raise _http_error(400)

        with pytest.raises(RuntimeError):
            always_bad_request()
        assert len(calls) == 1, "永久性错误被重试了"

    def test_rate_limit_is_retried(self):
        from core.retry import retry_api

        calls = []

        @retry_api(max_attempts=3, base_delay=0.001)
        def rate_limited():
            calls.append(1)
            raise _http_error(429)

        with pytest.raises(RuntimeError):
            rate_limited()
        assert len(calls) == 3

    def test_server_error_is_retried(self):
        from core.retry import is_retryable

        assert is_retryable(_http_error(503)) is True
        assert is_retryable(_http_error(500)) is True

    def test_unknown_error_is_retried(self):
        """网络层异常往往没有状态码，而它们最值得重试。"""
        from core.retry import is_retryable

        assert is_retryable(ConnectionError("连接被重置")) is True

    def test_retry_after_header_is_honored(self, monkeypatch):
        from core import retry as retry_mod

        slept = []
        monkeypatch.setattr(retry_mod.time, "sleep", lambda s: slept.append(s))

        @retry_mod.retry_api(max_attempts=2, base_delay=10.0)
        def limited():
            raise _http_error(429, retry_after=0.25)

        with pytest.raises(RuntimeError):
            limited()
        assert slept == [0.25], "未采信服务端给出的 Retry-After"

    def test_backoff_has_jitter(self, monkeypatch):
        """并发失败时若无抖动会造成惊群。"""
        from core import retry as retry_mod

        slept = []
        monkeypatch.setattr(retry_mod.time, "sleep", lambda s: slept.append(s))

        @retry_mod.retry_api(max_attempts=4, base_delay=1.0)
        def flaky():
            raise _http_error(503)

        with pytest.raises(RuntimeError):
            flaky()
        assert len(slept) == 3
        assert len(set(slept)) > 1, "退避间隔完全相同，说明没有抖动"
        assert all(0 < s <= 30 for s in slept)
