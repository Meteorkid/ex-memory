"""/keychain — 管理 macOS Keychain 中的 API Key。"""
from commands import register


def cmd_keychain(args: str):
    from core.keychain import cmd_keychain as _handler
    _handler(args)


register("keychain", cmd_keychain)
