"""API 调用重试与熔断。"""

import time
import functools
import logging

logger = logging.getLogger("ex-memory")


def retry_api(max_attempts: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
    """指数退避重试装饰器。"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_attempts:
                        delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                        logger.warning(
                            "%s 第 %d/%d 次失败: %s，%0.1fs 后重试",
                            func.__name__, attempt, max_attempts, e, delay,
                        )
                        time.sleep(delay)
            logger.error("%s 全部 %d 次重试失败: %s", func.__name__, max_attempts, last_error)
            raise last_error

        return wrapper

    return decorator
