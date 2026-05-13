"""重试装饰器 — 测试。"""

import pytest
from core.retry import retry_api


class TestRetryApi:
    def test_success_first_try(self):
        call_count = [0]

        @retry_api(max_attempts=3, base_delay=0.01)
        def succeed():
            call_count[0] += 1
            return "ok"

        result = succeed()
        assert result == "ok"
        assert call_count[0] == 1

    def test_retry_then_succeed(self):
        call_count = [0]

        @retry_api(max_attempts=3, base_delay=0.01)
        def flaky():
            call_count[0] += 1
            if call_count[0] < 3:
                raise ValueError("临时错误")
            return "recovered"

        result = flaky()
        assert result == "recovered"
        assert call_count[0] == 3

    def test_all_retries_exhausted(self):
        call_count = [0]

        @retry_api(max_attempts=3, base_delay=0.01)
        def always_fails():
            call_count[0] += 1
            raise RuntimeError("永远失败")

        with pytest.raises(RuntimeError, match="永远失败"):
            always_fails()
        assert call_count[0] == 3

    def test_preserves_function_metadata(self):
        @retry_api(max_attempts=2, base_delay=0.01)
        def my_func(x):
            """文档字符串。"""
            return x * 2

        assert my_func.__name__ == "my_func"
        assert my_func.__doc__ == "文档字符串。"
        assert my_func(5) == 10
