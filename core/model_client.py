"""账号模型客户端的异常边界：供应商响应不能泄露凭据或请求原文。"""

from types import SimpleNamespace


class ModelProviderError(RuntimeError):
    def __init__(self, status_code=None):
        self.status_code = status_code if isinstance(status_code, int) else None
        if self.status_code in (401, 403):
            message = "模型服务认证失败，请检查当前账号的 API Key"
        elif self.status_code == 429:
            message = "模型服务额度不足或请求过于频繁，请稍后重试"
        else:
            message = "模型服务暂时不可用，请检查账号配置后重试"
        super().__init__(message)


def provider_call(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except Exception as error:
        # 不保留异常文本、response 或异常链；其中可能包含 Key 和聊天原文。
        raise ModelProviderError(getattr(error, "status_code", None)) from None


def _safe_stream(stream):
    try:
        yield from stream
    except Exception as error:
        raise ModelProviderError(getattr(error, "status_code", None)) from None
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass  # 释放连接失败不得把供应商响应带回日志。


def account_client(client):
    """仅暴露业务使用的接口，普通响应与延迟发生的流式错误都经过脱敏。"""
    def create(**kwargs):
        result = provider_call(client.chat.completions.create, **kwargs)
        return _safe_stream(result) if kwargs.get("stream") else result

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
