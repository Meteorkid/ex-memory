"""短信验证码通道。

服务商未定，这里只定义接口，不绑任何一家 SDK。默认是开发用的
ConsoleProvider：把验证码打进日志而不真发短信。

🔴 生产环境必须注入真实 provider。未注入时 register 会拒绝放行，
不会退化成「不验证也能注册」——实名制不能靠默认值失守。
"""

import logging
import secrets
from typing import Optional, Protocol

logger = logging.getLogger("ex-memory")

CODE_LENGTH = 6
CODE_TTL_SECONDS = 300


class SmsProvider(Protocol):
    name: str

    def send(self, phone: str, code: str) -> bool: ...


class ConsoleProvider:
    """开发用：验证码只打日志。生产环境注入真实实现前不可放行注册。"""

    name = "console"

    def send(self, phone: str, code: str) -> bool:
        logger.warning("【开发模式】短信验证码 phone=%s code=%s", phone, code)
        return True


_provider: Optional[SmsProvider] = None


def set_provider(provider: Optional[SmsProvider]) -> None:
    global _provider
    _provider = provider


def get_provider() -> Optional[SmsProvider]:
    return _provider


def generate_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))


def send_code(phone: str, code: str) -> bool:
    if _provider is None:
        logger.error("短信通道未配置，无法发送验证码")
        return False
    try:
        return _provider.send(phone, code)
    except Exception as e:  # noqa: BLE001 — 通道故障不得让注册流程崩掉
        logger.error("短信发送失败 provider=%s: %s", _provider.name, e)
        return False
