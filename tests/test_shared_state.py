"""共享状态：限流、失效广播与 Redis 降级（FR-035 / D-12）。

带 redis 标记的用例需要本机 Redis；不可用时自动跳过，
不让 CI 因为缺中间件而变红。
"""

import time
import uuid

import pytest

from core import kv

REDIS_URL = "redis://localhost:6379/15"


def _redis_available() -> bool:
    try:
        kv.RedisBackend(REDIS_URL)
        return True
    except Exception:
        return False


requires_redis = pytest.mark.skipif(not _redis_available(), reason="本机无可用 Redis")


@pytest.fixture(autouse=True)
def clean_kv():
    kv.reset_for_tests()
    yield
    kv.reset_for_tests()


@pytest.fixture
def temp_handler():
    """注册临时失效处理器，用完只摘自己那个。

    不能用 _local_handlers.clear()：server.routes 在导入时注册了一个
    常驻处理器，清空会把它一起干掉，后面的用例就再也收不到失效通知。
    """
    added = []

    def register(handler):
        kv.on_invalidate(handler)
        added.append(handler)
        return handler

    yield register
    for handler in added:
        if handler in kv._local_handlers:
            kv._local_handlers.remove(handler)


class TestSlidingWindow:
    def test_counts_within_window(self):
        kv.configure("")
        key = f"t:{uuid.uuid4().hex}"
        assert [kv.incr_window(key, 60) for _ in range(3)] == [1, 2, 3]

    def test_old_hits_fall_out_of_window(self):
        kv.configure("")
        key = f"t:{uuid.uuid4().hex}"
        kv.incr_window(key, 1)
        time.sleep(1.05)
        assert kv.incr_window(key, 1) == 1

    def test_keys_are_independent(self):
        kv.configure("")
        assert kv.incr_window("t:a", 60) == 1
        assert kv.incr_window("t:b", 60) == 1


class TestDegradation:
    def test_unreachable_redis_falls_back_not_fails(self):
        """🔴 降级而非失效：退化成每副本各自限流，仍好过完全不限流。"""
        kv.configure("redis://localhost:6399/0")
        assert kv.backend().name == "memory"
        assert kv.is_degraded() is True
        assert kv.incr_window("t:x", 60) == 1  # 仍然可用

    def test_empty_url_uses_memory_without_marking_degraded(self):
        kv.configure("")
        assert kv.backend().name == "memory"
        assert kv.is_degraded() is False


class TestInvalidationBroadcast:
    def test_local_handlers_are_called(self, temp_handler):
        kv.configure("")
        seen = []
        temp_handler(seen.append)
        kv.broadcast_invalidate("xiaoyu")
        assert seen == ["xiaoyu"]

    def test_failing_handler_does_not_block_others(self, temp_handler):
        kv.configure("")
        seen = []

        def boom(slug):
            raise RuntimeError("处理失败")

        temp_handler(boom)
        temp_handler(seen.append)
        kv.broadcast_invalidate("xiaoyu")
        assert seen == ["xiaoyu"]

    def test_listener_is_noop_on_memory_backend(self):
        kv.configure("")
        assert kv.start_invalidation_listener() is False


@requires_redis
class TestAgainstRealRedis:
    def test_window_is_shared_across_backend_instances(self):
        """两个后端实例代表两个副本，必须共用同一份窗口计数。"""
        key = f"t:{uuid.uuid4().hex}"
        replica_a = kv.RedisBackend(REDIS_URL)
        replica_b = kv.RedisBackend(REDIS_URL)
        assert replica_a.incr_window(key, 60) == 1
        assert replica_b.incr_window(key, 60) == 2, "限流额度按副本数翻倍了"
        replica_a.delete(key)

    def test_value_roundtrip_and_ttl(self):
        key = f"t:{uuid.uuid4().hex}"
        backend = kv.RedisBackend(REDIS_URL)
        backend.set(key, "v", ttl_seconds=1)
        assert backend.get(key) == "v"
        time.sleep(1.2)
        assert backend.get(key) is None

    def test_invalidation_reaches_another_replica(self, temp_handler):
        """D-12：一个副本清缓存，其余副本必须跟着清。"""
        kv.configure(REDIS_URL)
        received = []
        temp_handler(received.append)
        assert kv.start_invalidation_listener() is True
        time.sleep(0.3)  # 等订阅建立

        publisher = kv.RedisBackend(REDIS_URL)
        publisher.publish(kv.INVALIDATION_CHANNEL, "from-other-replica")

        deadline = time.time() + 3
        while time.time() < deadline and "from-other-replica" not in received:
            time.sleep(0.05)
        assert "from-other-replica" in received, "跨副本失效通知没收到"

    def test_engine_cache_is_evicted_by_remote_invalidation(self):
        """端到端：远端广播应清掉本副本的引擎缓存。"""
        import server.routes as routes

        kv.configure(REDIS_URL)
        kv.start_invalidation_listener()
        time.sleep(0.3)

        routes._engine_cache[(1, "target")] = object()
        routes._engine_cache[(2, "target")] = object()
        routes._engine_cache[(1, "keep")] = object()

        kv.RedisBackend(REDIS_URL).publish(kv.INVALIDATION_CHANNEL, "target")

        deadline = time.time() + 3
        while time.time() < deadline and any(
            k[1] == "target" for k in routes._engine_cache.keys()
        ):
            time.sleep(0.05)

        assert not any(k[1] == "target" for k in routes._engine_cache.keys())
        assert (1, "keep") in routes._engine_cache
        routes._engine_cache.clear()
