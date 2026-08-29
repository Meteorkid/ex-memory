"""localhost 控制面的安全原语。"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1", "testserver"})


def normalize_host(raw_host: str) -> str:
    """去除 Host 端口，保留 IPv6 回环地址。"""
    host = raw_host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end >= 0 else host
    return host.split(":", 1)[0]


def is_loopback_host(raw_host: str) -> bool:
    return normalize_host(raw_host) in LOOPBACK_HOSTS


def validate_origin(origin: str | None, allowed_origins: frozenset[str]) -> bool:
    return bool(origin and origin.rstrip("/") in allowed_origins)


@dataclass(frozen=True)
class LaunchTicket:
    token: str
    expires_at: float


class OneTimeTicketStore:
    """内存一次性票据：不落盘，消费后立即失效。"""

    def __init__(self, ttl_seconds: int = 90):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须大于 0")
        self._ttl_seconds = ttl_seconds
        self._tickets: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> LaunchTicket:
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        expires_at = now + self._ttl_seconds
        with self._lock:
            self._purge(now)
            self._tickets[token] = expires_at
        return LaunchTicket(token=token, expires_at=expires_at)

    def consume(self, token: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            expires_at = self._tickets.pop(token, None)
        return expires_at is not None and expires_at > now

    def _purge(self, now: float) -> None:
        expired = [token for token, expiry in self._tickets.items() if expiry <= now]
        for token in expired:
            self._tickets.pop(token, None)


@dataclass(frozen=True)
class LocalSession:
    token: str
    csrf_token: str
    task_id: str
    workflow_task_id: str
    expires_at: float


class LocalSessionStore:
    """localhost 页面会话，与公共网站控制面完全隔离。"""

    def __init__(self, ttl_seconds: int = 8 * 60 * 60):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须大于 0")
        self._ttl_seconds = ttl_seconds
        self._sessions: dict[str, LocalSession] = {}
        self._lock = threading.Lock()

    def issue(self, task_id: str) -> LocalSession:
        now = time.monotonic()
        session = LocalSession(
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            task_id=task_id,
            workflow_task_id=task_id,
            expires_at=now + self._ttl_seconds,
        )
        with self._lock:
            self._purge(now)
            self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> LocalSession | None:
        if not token:
            return None
        now = time.monotonic()
        with self._lock:
            self._purge(now)
            session = self._sessions.get(token)
        return session if session and session.expires_at > now else None

    def bind_workflow(self, token: str, workflow_task_id: str) -> LocalSession:
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise KeyError(token)
            rebound = LocalSession(
                token=session.token,
                csrf_token=session.csrf_token,
                task_id=session.task_id,
                workflow_task_id=workflow_task_id,
                expires_at=session.expires_at,
            )
            self._sessions[token] = rebound
            return rebound

    def _purge(self, now: float) -> None:
        expired = [
            token
            for token, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for token in expired:
            self._sessions.pop(token, None)
