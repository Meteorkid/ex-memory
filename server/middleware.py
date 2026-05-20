"""FastAPI 中间件：CORS、限流、认证、请求日志。"""

import os
import time
import uuid
import logging
import threading
from collections import defaultdict
from fastapi import Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger("ex-memory")
security = HTTPBearer(auto_error=False)


def setup_cors(app):
    origins_str = os.getenv("CORS_ORIGINS", "http://localhost:8000,http://localhost:7860")
    allow_origins = [o.strip() for o in origins_str.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def require_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """认证依赖项：验证 Bearer token。"""
    if credentials is None:
        raise HTTPException(status_code=401, detail="需要认证")
    token = credentials.credentials
    from server.auth import validate_token
    user_id = validate_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    return user_id


def optional_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """可选认证：不强制，但如果有 token 则验证。"""
    if credentials is None:
        return None
    from server.auth import validate_token
    return validate_token(credentials.credentials)


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP；仅在 TRUSTED_PROXY 时信任 X-Forwarded-For。"""
    from config import TRUSTED_PROXY
    if TRUSTED_PROXY:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


class RateLimiter:
    """简单的内存限流器，支持代理穿透和定期清理。"""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds
        self._store: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()
        self._lock = threading.Lock()

    async def __call__(self, request: Request, call_next):
        client_ip = _get_client_ip(request)
        now = time.time()

        with self._lock:
            # 定期清理过期 IP 条目，防止 _store 无限增长
            if now - self._last_cleanup > 300:  # 每 5 分钟
                self._cleanup(now)
                self._last_cleanup = now

            # 清理该 IP 的过期记录
            window_start = now - self.window
            self._store[client_ip] = [
                t for t in self._store[client_ip] if t > window_start
            ]

            if len(self._store[client_ip]) >= self.max_requests:
                logger.warning("rate limit hit for %s", client_ip)
                return JSONResponse(
                    status_code=429,
                    content={"detail": "请求过于频繁，请稍后再试"},
                )

            self._store[client_ip].append(now)

        response = await call_next(request)
        return response

    def _cleanup(self, now: float):
        """清除所有过期的 IP 条目。"""
        window_start = now - self.window
        stale = [ip for ip, ts_list in self._store.items()
                  if not any(t > window_start for t in ts_list)]
        for ip in stale:
            del self._store[ip]
        if stale:
            logger.debug("rate limiter cleaned %d stale IP entries", len(stale))


class LoginRateLimiter:
    """登录接口独立限流：5 次/分钟/用户名，15 次/分钟/IP。"""

    def __init__(self, max_per_user: int = 5, max_per_ip: int = 15, window_seconds: int = 60):
        self.max_per_user = max_per_user
        self.max_per_ip = max_per_ip
        self.window = window_seconds
        self._user_store: dict[str, list[float]] = defaultdict(list)
        self._ip_store: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.time()
        self._lock = threading.Lock()

    def check(self, username: str, client_ip: str) -> None:
        now = time.time()
        with self._lock:
            if now - self._last_cleanup > 300:
                self._cleanup(now)
                self._last_cleanup = now

            window_start = now - self.window

            # 按用户名限流
            self._user_store[username] = [
                t for t in self._user_store[username] if t > window_start
            ]
            if len(self._user_store[username]) >= self.max_per_user:
                logger.warning("login rate limit (user) hit for %s", username)
                raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")

            # 按 IP 限流
            self._ip_store[client_ip] = [
                t for t in self._ip_store[client_ip] if t > window_start
            ]
            if len(self._ip_store[client_ip]) >= self.max_per_ip:
                logger.warning("login rate limit (IP) hit for %s", client_ip)
                raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")

            self._user_store[username].append(now)
            self._ip_store[client_ip].append(now)

    def _cleanup(self, now: float):
        window_start = now - self.window
        for store in (self._user_store, self._ip_store):
            stale = [k for k, ts_list in store.items()
                     if not any(t > window_start for t in ts_list)]
            for k in stale:
                del store[k]


class RequestLoggingMiddleware:
    """请求日志中间件：记录 method、path、status、duration_ms、request_id。"""

    async def __call__(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
        request.state.request_id = request_id

        start = time.time()
        response = await call_next(request)
        duration_ms = int((time.time() - start) * 1000)

        logger.info(
            "request_id=%s method=%s path=%s status=%d duration_ms=%d",
            request_id, request.method, request.url.path, response.status_code, duration_ms,
        )
        response.headers["X-Request-ID"] = request_id
        return response


class SecurityHeadersMiddleware:
    """为 API 和 Web 客户端补齐基础安全响应头。"""

    async def __call__(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(self), geolocation=()")

        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")

        return response
