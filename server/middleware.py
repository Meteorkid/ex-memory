"""FastAPI 中间件：CORS、限流、认证、请求日志。"""

import hmac
import os
import time
import uuid
import logging
from fastapi import Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger("ex-memory")
security = HTTPBearer(auto_error=False)


def setup_cors(app):
    origins_str = os.getenv(
        "CORS_ORIGINS", "http://localhost:8000,http://localhost:7860"
    )
    allow_origins = [o.strip() for o in origins_str.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )


def _proxy_user_id(request: Request) -> int:
    """验证反向代理身份头，并映射为本地整数用户 ID。"""
    import config

    expected = config.METEOR_STORE_PROXY_TOKEN
    actual = request.headers.get("X-Ex-Memory-Proxy-Token", "")
    external_user_id = request.headers.get("X-Ex-Memory-User-Id", "")
    if (
        not expected
        or not hmac.compare_digest(actual, expected)
        or not external_user_id
    ):
        raise HTTPException(status_code=401, detail="需要 Meteor Store 登录")

    from server.auth import get_or_create_external_user_id

    try:
        return get_or_create_external_user_id("meteor-store", external_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="代理身份无效") from exc


def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """认证依赖项：代理模式与 Bearer 模式严格二选一。"""
    import config

    if config.METEOR_STORE_SSO_ENABLED:
        return _proxy_user_id(request)
    if credentials is None:
        raise HTTPException(status_code=401, detail="需要认证")
    token = credentials.credentials
    from server.auth import validate_token

    user_id = validate_token(token)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    return user_id


def require_admin(user_id: int = Depends(require_auth)) -> int:
    """管理员权限校验。

    复核队列里是用户最脆弱时刻的记录与被模拟者的投诉，任何登录用户都能
    翻看是不可接受的。users.role 字段一直存在但全仓无人读，这里第一次
    真正用上它。

    刻意不做「第一个用户自动是管理员」这类便利逻辑——权限提升必须是
    显式动作，用 `python run.py` 的 /grant-admin 命令授予。
    """
    from server.auth import get_user_role

    if get_user_role(user_id) != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user_id


def optional_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """可选认证：不强制，但如果有 token 则验证。"""
    import config

    if config.METEOR_STORE_SSO_ENABLED:
        try:
            return _proxy_user_id(request)
        except HTTPException:
            return None
    if credentials is None:
        return None
    from server.auth import validate_token

    return validate_token(credentials.credentials)


# 白名单缺失的告警只打一次，避免每个请求刷屏
_warned_empty_proxy_whitelist = False


def _get_client_ip(request: Request) -> str:
    """获取客户端 IP；仅当 TRUSTED_PROXY 开启且直连来源在白名单内才信任 X-Forwarded-For。"""
    global _warned_empty_proxy_whitelist
    from config import TRUSTED_PROXY, TRUSTED_PROXY_IPS

    direct_ip = request.client.host if request.client else "unknown"
    if not TRUSTED_PROXY:
        return direct_ip

    # fail-closed：白名单为空说明配置不完整，此时信任 XFF 会让任何人伪造头绕过限流
    if not TRUSTED_PROXY_IPS:
        if not _warned_empty_proxy_whitelist:
            logger.warning(
                "TRUSTED_PROXY 已启用但 TRUSTED_PROXY_IPS 为空，"
                "已忽略 X-Forwarded-For 并按直连 IP 限流；请补齐白名单"
            )
            _warned_empty_proxy_whitelist = True
        return direct_ip

    # 直连来源不是可信代理，说明请求绕过了代理，其 XFF 不可信
    if direct_ip not in TRUSTED_PROXY_IPS:
        return direct_ip

    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return direct_ip


class RateLimiter:
    """全局限流。计数走共享 KV，多副本共用同一份窗口。

    此前是进程内 dict：启动第二个 worker 就等于把限流额度翻倍。
    """

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window = window_seconds

    async def __call__(self, request: Request, call_next):
        from core import kv

        client_ip = _get_client_ip(request)
        hits = kv.incr_window(f"rl:ip:{client_ip}", self.window)
        if hits > self.max_requests:
            logger.warning("rate limit hit for %s", client_ip)
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试"},
            )
        return await call_next(request)


class LoginRateLimiter:
    """登录接口独立限流：5 次/分钟/用户名，15 次/分钟/IP。

    同样走共享 KV——按用户名限流若只在单副本内生效，暴力破解换个
    连接落到另一个副本就绕过去了。
    """

    def __init__(
        self, max_per_user: int = 5, max_per_ip: int = 15, window_seconds: int = 60
    ):
        self.max_per_user = max_per_user
        self.max_per_ip = max_per_ip
        self.window = window_seconds

    def check(self, username: str, client_ip: str) -> None:
        from core import kv

        if kv.incr_window(f"rl:login:user:{username}", self.window) > self.max_per_user:
            logger.warning("login rate limit (user) hit for %s", username)
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")
        if kv.incr_window(f"rl:login:ip:{client_ip}", self.window) > self.max_per_ip:
            logger.warning("login rate limit (IP) hit for %s", client_ip)
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试")


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
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        response.headers["X-Request-ID"] = request_id
        return response
