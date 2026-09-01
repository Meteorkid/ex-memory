"""带上限与 TTL 的线程安全缓存。

`_engine_cache` 与 `_session_counters` 原本是无上限的裸 dict：每个聊过的
镜像都会永久驻留一个 ChatEngine（含数万字符人格文本与向量库客户端），
长期运行必然 OOM。这里提供统一实现，并支持按 slug 批量失效——
镜像删除/更新/回滚时要清掉所有用户对该 slug 的缓存。
"""

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Iterator, Optional


class BoundedCache:
    """LRU + TTL 缓存。键为任意可哈希值，淘汰时可回调释放资源。"""

    def __init__(
        self,
        maxsize: int = 128,
        ttl_seconds: Optional[float] = None,
        on_evict: Optional[Callable[[Any, Any], None]] = None,
    ):
        if maxsize < 1:
            raise ValueError("maxsize 必须为正")
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._on_evict = on_evict
        self._data: OrderedDict[Any, tuple[Any, float]] = OrderedDict()
        self._lock = threading.RLock()

    def _expired(self, stored_at: float) -> bool:
        return self._ttl is not None and (time.monotonic() - stored_at) > self._ttl

    def get(self, key: Any) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, stored_at = entry
            if self._expired(stored_at):
                del self._data[key]
                self._evict(key, value)
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data.pop(key)
            self._data[key] = (value, time.monotonic())
            while len(self._data) > self._maxsize:
                old_key, (old_value, _) = self._data.popitem(last=False)
                self._evict(old_key, old_value)

    def get_or_create(self, key: Any, factory: Callable[[], Any]) -> Any:
        """取不到则创建。factory 在锁外执行，避免慢构造阻塞其他键。"""
        existing = self.get(key)
        if existing is not None:
            return existing
        created = factory()
        with self._lock:
            # 双检：并发下别的线程可能已经放进去了，用它的以免出现两份实例
            current = self._data.get(key)
            if current is not None and not self._expired(current[1]):
                self._data.move_to_end(key)
                return current[0]
            self._data[key] = (created, time.monotonic())
            while len(self._data) > self._maxsize:
                old_key, (old_value, _) = self._data.popitem(last=False)
                self._evict(old_key, old_value)
            return created

    def __setitem__(self, key: Any, value: Any) -> None:
        self.set(key, value)

    def pop(self, key: Any) -> Optional[Any]:
        with self._lock:
            entry = self._data.pop(key, None)
            if entry is None:
                return None
            self._evict(key, entry[0])
            return entry[0]

    def evict_where(self, predicate: Callable[[Any], bool]) -> int:
        """按键谓词批量淘汰，返回淘汰数量。"""
        with self._lock:
            keys = [k for k in self._data if predicate(k)]
            for key in keys:
                value, _ = self._data.pop(key)
                self._evict(key, value)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            for key, (value, _) in list(self._data.items()):
                self._evict(key, value)
            self._data.clear()

    def keys(self) -> list[Any]:
        with self._lock:
            return list(self._data.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, key: Any) -> bool:
        return self.get(key) is not None

    def __iter__(self) -> Iterator[Any]:
        return iter(self.keys())

    def _evict(self, key: Any, value: Any) -> None:
        if self._on_evict is None:
            return
        try:
            self._on_evict(key, value)
        except Exception:  # noqa: BLE001 — 释放失败不应影响缓存本身
            import logging

            logging.getLogger("ex-memory").warning("缓存淘汰回调失败", exc_info=True)
