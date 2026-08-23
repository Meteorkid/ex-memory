"""由 LLDB 加载的微信 4.1.12 PBKDF2 一次性捕获回调。"""

from __future__ import annotations

import binascii


_PASSWORD_LENGTH = 32
_SALT_LENGTH = 16
_KDF_ITERATIONS = 256_000
_captured_password: bytearray | None = None


def is_wechat_sqlcipher_kdf(password_length: int, salt_length: int, rounds: int) -> bool:
    return (
        password_length == _PASSWORD_LENGTH
        and salt_length == _SALT_LENGTH
        and rounds == _KDF_ITERATIONS
    )


def _register_value(frame, name: str) -> int:
    register = frame.FindRegister(name)
    if not register.IsValid():
        return -1
    return register.GetValueAsUnsigned()


def capture_callback(frame, _breakpoint_location, _internal_dict) -> bool:
    """只在符合微信 SQLCipher 参数模型时停止进程。"""
    global _captured_password
    password_length = _register_value(frame, "x2")
    salt_length = _register_value(frame, "x4")
    rounds = _register_value(frame, "x6")
    if not is_wechat_sqlcipher_kdf(password_length, salt_length, rounds):
        return False

    process = frame.GetThread().GetProcess()
    import lldb

    read_error = lldb.SBError()
    raw = process.ReadMemory(_register_value(frame, "x1"), password_length, read_error)
    if not read_error.Success() or len(raw) != password_length:
        return False
    _captured_password = bytearray(raw)
    return True


def install(debugger, _command, result, _internal_dict) -> None:
    target = debugger.GetSelectedTarget()
    breakpoint = target.BreakpointCreateByName("CCKeyDerivationPBKDF")
    if not breakpoint.IsValid() or breakpoint.GetNumLocations() == 0:
        result.SetError("CCKeyDerivationPBKDF breakpoint unavailable")
        return
    breakpoint.SetScriptCallbackFunction(f"{__name__}.capture_callback")


def emit(_debugger, _command, result, _internal_dict) -> None:
    global _captured_password
    if _captured_password is None:
        result.SetError("matching PBKDF2 call not captured")
        return
    result.AppendMessage("WECHAT_PBKDF2_PASSWORD " + binascii.hexlify(_captured_password).decode("ascii"))
    for index in range(len(_captured_password)):
        _captured_password[index] = 0
    _captured_password = None


def __lldb_init_module(debugger, _internal_dict) -> None:
    debugger.HandleCommand(f"command script add -f {__name__}.install wechat-key-install")
    debugger.HandleCommand(f"command script add -f {__name__}.emit wechat-key-emit")
