"""共享 KV 与发布订阅：把进程内状态搬出去，让服务能多副本。

限流窗口、登录失败计数、验证码、会话用量原本都在进程内字典里，
多副本下会各算各的：限流额度翻倍、用量统计对不上、验证码换台机器就失效。

**故障策略是降级而非失效**：Redis 不可用时自动退回进程内实现。
限流退化成「每副本各自限流」——那正是今天的行为，仍比「完全不限流」
或「服务直接不可用」都好。降级会打错误日志，不会静默发生。
"""

import logging
import threading
import time
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger("ex-memory")


class KVBackend(Protocol):
    name: str

    def incr_window(self, key: str, window_seconds: int) -> int: ...
    def get(self, key: str) -> Optional[str]: ...
    def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def incr_by(
        self, key: str, amount: int, ttl_seconds: Optional[int] = None
    ) -> int: ...
    def publish(self, channel: str, message: str) -> None: ...


class MemoryBackend:
    """进程内实现。单副本部署与测试用，也是 Redis 故障时的降级目标。"""

    name = "memory"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, tuple[str, Optional[float]]] = {}
        self._windows: dict[str, list[float]] = {}
        self._subscribers: list[Callable[[str, str], None]] = []

    def _alive(self, expires_at: Optional[float]) -> bool:
        return expires_at is None or time.time() < expires_at

    def incr_window(self, key: str, window_seconds: int) -> int:
        now = time.time()
        with self._lock:
            hits = [t for t in self._windows.get(key, []) if now - t < window_seconds]
            hits.append(now)
            self._windows[key] = hits
            return len(hits)

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if not self._alive(expires_at):
                del self._values[key]
                return None
            return value

    def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
        with self._lock:
            expires_at = time.time() + ttl_seconds if ttl_seconds else None
            self._values[key] = (value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)
            self._windows.pop(key, None)

    def incr_by(self, key: str, amount: int, ttl_seconds: Optional[int] = None) -> int:
        with self._lock:
            current = int(self.get(key) or 0) + amount
            self.set(key, str(current), ttl_seconds)
            return current

    def publish(self, channel: str, message: str) -> None:
        # 单进程下「广播」就是直接回调本地订阅者
        for handler in list(self._subscribers):
            try:
                handler(channel, message)
            except Exception:  # noqa: BLE001
                logger.warning("本地订阅回调失败", exc_info=True)

    def subscribe(self, handler: Callable[[str, str], None]) -> None:
        with self._lock:
            self._subscribers.append(handler)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._windows.clear()


class RedisBackend:
    """Redis 实现。滑动窗口用 ZSET，保证多副本共享同一份计数。"""

    name = "redis"

    def __init__(self, url: str):
        import redis

        # redis-py 的 from_url 在 decode_responses 分支上类型标注不一致，
        # 标成 Any 而不是到处 cast
        self._client: Any = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=1.0, socket_connect_timeout=1.0
        )
        self._client.ping()

    def incr_window(self, key: str, window_seconds: int) -> int:
        now = time.time()
        pipe = self._client.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zadd(key, {f"{now}:{id(pipe)}": now})
        pipe.zcard(key)
        pipe.expire(key, window_seconds + 1)
        return int(pipe.execute()[2])

    def get(self, key: str) -> Optional[str]:
        return self._client.get(key)

    def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
        self._client.set(key, value, ex=ttl_seconds)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def incr_by(self, key: str, amount: int, ttl_seconds: Optional[int] = None) -> int:
        pipe = self._client.pipeline()
        pipe.incrby(key, amount)
        if ttl_seconds:
            pipe.expire(key, ttl_seconds)
        return int(pipe.execute()[0])

    def publish(self, channel: str, message: str) -> None:
        self._client.publish(channel, message)

    def raw(self) -> Any:
        return self._client


_backend: Optional[KVBackend] = None
_fallback = MemoryBackend()
_degraded = False


def configure(url: str = "") -> KVBackend:
    """按配置选择后端。URL 为空或连接失败时退回进程内实现。"""
    global _backend, _degraded
    if not url:
        _backend = _fallback
        _degraded = False
        logger.info("共享状态使用进程内实现（未配置 REDIS_URL，仅适用于单副本）")
        return _backend
    try:
        _backend = RedisBackend(url)
        _degraded = False
        logger.info("共享状态使用 Redis")
    except Exception as e:  # noqa: BLE001 — 连不上就降级，不让服务起不来
        logger.error("Redis 连接失败（%s），降级为进程内实现，多副本下状态不共享", e)
        _backend = _fallback
        _degraded = True
    return _backend


def backend() -> KVBackend:
    if _backend is None:
        import config

        configure(getattr(config, "REDIS_URL", ""))
    return _backend  # type: ignore[return-value]


def is_degraded() -> bool:
    return _degraded


def reset_for_tests() -> None:
    global _backend, _degraded
    _backend = None
    _degraded = False
    _fallback.clear()


def _call(method: str, *args, **kwargs):
    """调用后端，失败时降级到进程内实现并告警。

    降级是有意的：限流退化成每副本各自限流仍好过完全不限流。
    """
    global _degraded
    current = backend()
    try:
        return getattr(current, method)(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        if not _degraded:
            _degraded = True
            logger.error("共享状态后端不可用（%s），本次降级到进程内实现", e)
        return getattr(_fallback, method)(*args, **kwargs)


def incr_window(key: str, window_seconds: int) -> int:
    return _call("incr_window", key, window_seconds)


def get(key: str) -> Optional[str]:
    return _call("get", key)


def set(key: str, value: str, ttl_seconds: Optional[int] = None) -> None:  # noqa: A001
    _call("set", key, value, ttl_seconds)


def delete(key: str) -> None:
    _call("delete", key)


def incr_by(key: str, amount: int, ttl_seconds: Optional[int] = None) -> int:
    return _call("incr_by", key, amount, ttl_seconds)


def publish(channel: str, message: str) -> None:
    _call("publish", channel, message)


# ── 跨副本失效广播 ──
#
# 引擎缓存持有活的 Python 对象，没法放进 Redis 共享。多副本下的正确做法是
# 广播失效：一个副本清了缓存，其余副本收到通知后各自清掉本地那份。
# 不广播的后果是用户纠正「ta 不会这样」只在一个副本生效，其余继续用旧人格。

INVALIDATION_CHANNEL = "ex-memory:invalidate"

_listener_thread: Optional[threading.Thread] = None
_local_handlers: list[Callable[[str], None]] = []


def on_invalidate(handler: Callable[[str], None]) -> None:
    """注册失效处理器。参数为被失效的 slug。"""
    _local_handlers.append(handler)


def broadcast_invalidate(slug: str) -> None:
    """广播一个 slug 的失效。本地先清，再通知其他副本。"""
    for handler in list(_local_handlers):
        try:
            handler(slug)
        except Exception:  # noqa: BLE001
            logger.warning("本地失效处理失败 slug=%s", slug, exc_info=True)
    current = backend()
    if getattr(current, "name", "") == "redis":
        try:
            current.publish(INVALIDATION_CHANNEL, slug)
        except Exception as e:  # noqa: BLE001 — 广播失败不能影响本地失效
            logger.error("失效广播发送失败 slug=%s: %s", slug, e)


def start_invalidation_listener() -> bool:
    """启动订阅线程。仅 Redis 后端有意义，返回是否真的启动了。"""
    global _listener_thread
    current = backend()
    if getattr(current, "name", "") != "redis":
        return False
    if _listener_thread is not None and _listener_thread.is_alive():
        return True

    def _listen() -> None:
        try:
            client = getattr(current, "raw")()
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(INVALIDATION_CHANNEL)
            for message in pubsub.listen():
                slug = message.get("data")
                if not slug:
                    continue
                for handler in list(_local_handlers):
                    try:
                        handler(slug)
                    except Exception:  # noqa: BLE001
                        logger.warning("失效处理失败 slug=%s", slug, exc_info=True)
        except Exception as e:  # noqa: BLE001 — 订阅断了只降级，不拖垮服务
            logger.error("失效订阅中断（%s），本副本将不再收到跨副本失效通知", e)

    _listener_thread = threading.Thread(
        target=_listen, name="kv-invalidation", daemon=True
    )
    _listener_thread.start()
    logger.info("已启动跨副本缓存失效订阅")
    return True
