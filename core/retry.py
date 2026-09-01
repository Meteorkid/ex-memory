"""API 调用重试与熔断。

原实现捕获裸 Exception 并无差别重试：400、401、内容策略拒绝这类**永久性
错误**也会重试 3 次，白白浪费 3 秒与 3 倍配额；且无抖动（并发下惊群）、
不读 Retry-After。这里按错误类型区分，只重试真正可能自愈的失败。
"""

import functools
import logging
import random
import time
from typing import Optional

logger = logging.getLogger("ex-memory")

# 可重试的 HTTP 状态：限流与服务端故障
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
# 明确不可重试：重试只会重复失败并消耗配额
PERMANENT_STATUS = frozenset({400, 401, 403, 404, 405, 413, 422})


def _status_of(error: Exception) -> Optional[int]:
    """尽量取出 HTTP 状态码。OpenAI SDK 的异常带 status_code。"""
    for attr in ("status_code", "status", "http_status"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _retry_after_of(error: Exception) -> Optional[float]:
    """读取服务端给出的 Retry-After（秒）。"""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None  # HTTP-date 形式不解析，退回指数退避


def is_retryable(error: Exception) -> bool:
    """判断错误是否值得重试。

    未知错误按可重试处理：网络层异常往往没有状态码，而它们恰恰是重试
    最有效的场景。已知的永久性状态则明确排除。
    """
    status = _status_of(error)
    if status is None:
        return True
    if status in PERMANENT_STATUS:
        return False
    if status in RETRYABLE_STATUS:
        return True
    # 其余 4xx 视为永久，5xx 视为可重试
    return status >= 500


def retry_api(max_attempts: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
    """指数退避重试装饰器，带抖动、错误分类与 Retry-After 支持。"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if not is_retryable(e):
                        logger.warning(
                            "%s 遇到不可重试的错误（status=%s），直接失败: %s",
                            func.__name__,
                            _status_of(e),
                            e,
                        )
                        raise
                    if attempt >= max_attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    server_hint = _retry_after_of(e)
                    if server_hint is not None:
                        delay = min(server_hint, max_delay)
                    else:
                        # 抖动：并发失败时避免同时重试造成惊群
                        delay *= 0.5 + random.random()
                    logger.warning(
                        "%s 第 %d/%d 次失败: %s，%0.1fs 后重试",
                        func.__name__,
                        attempt,
                        max_attempts,
                        e,
                        delay,
                    )
                    time.sleep(delay)
            logger.error(
                "%s 全部 %d 次重试失败: %s", func.__name__, max_attempts, last_error
            )
            raise last_error

        return wrapper

    return decorator
