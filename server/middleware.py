"""FastAPI 中间件：CORS、限流、认证。"""

import time
import logging
from collections import defaultdict
from fastapi import Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger("ex-memory")
security = HTTPBearer(auto_error=False)


def setup_cors(app):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
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
    """获取客户端真实 IP，优先从 X-Forwarded-For 取。"""
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

    async def __call__(self, request: Request, call_next):
        client_ip = _get_client_ip(request)
        now = time.time()

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
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

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
