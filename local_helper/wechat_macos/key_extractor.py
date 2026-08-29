"""微信 4.1.12 当前账号 SQLCipher 密钥提取。"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from local_helper.wechat_macos.page_verifier import (
    derive_sqlcipher4_key,
    verify_sqlcipher4_page,
)
from local_helper.wechat_macos.sip import SIPStatus


_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_PASSWORD_LINE = re.compile(r"^WECHAT_PBKDF2_PASSWORD ([0-9a-f]{64})$", re.MULTILINE)
_PID_LINE = re.compile(r"^WECHAT_PID ([0-9]+)$", re.MULTILINE)


class KeyExtractionError(RuntimeError):
    """密钥提取失败，消息不包含密码、key 或 salt。"""


@dataclass(frozen=True)
class WeChatKeyPair:
    key_hex: str
    salt_hex: str

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.key_hex):
            raise ValueError("密钥格式无效")
        if not _HEX_32.fullmatch(self.salt_hex):
            raise ValueError("salt 格式无效")


@dataclass(frozen=True)
class CapturedAccountKeys:
    wechat_pid: int
    keys: tuple[WeChatKeyPair, ...]


def collect_database_salts(databases: Iterable[Path]) -> tuple[str, ...]:
    salts: set[str] = set()
    for path in databases:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(16)
        except OSError:
            continue
        if len(raw) == 16 and raw != b"SQLite format 3\x00":
            salts.add(raw.hex())
    return tuple(sorted(salts))


def derive_verified_key_pairs(
    password: bytes, databases: Iterable[Path]
) -> tuple[WeChatKeyPair, ...]:
    """用一次捕获的账号密码验证该账号下全部可读加密数据库。"""
    if len(password) != 32:
        return ()
    pages_by_salt: dict[bytes, list[bytes]] = {}
    for path in databases:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with path.open("rb") as stream:
                page = stream.read(4096)
        except OSError:
            continue
        if len(page) != 4096 or page.startswith(b"SQLite format 3\x00"):
            continue
        pages_by_salt.setdefault(page[:16], []).append(page)

    pairs: list[WeChatKeyPair] = []
    for salt, pages in sorted(pages_by_salt.items()):
        raw_key = derive_sqlcipher4_key(password, salt)
        if all(verify_sqlcipher4_page(raw_key, page) for page in pages):
            pairs.append(WeChatKeyPair(key_hex=raw_key.hex(), salt_hex=salt.hex()))
    return tuple(pairs)


def extract_current_account_keys(
    *,
    launcher: Path,
    capture_module: Path,
    databases: Iterable[Path],
    sip_status: SIPStatus,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> CapturedAccountKeys:
    if sip_status is not SIPStatus.DISABLED:
        raise KeyExtractionError("仅能在用户已手动关闭 SIP 的专家模式中提取密钥")
    launcher = _verified_executable(launcher)
    capture_module = _verified_file(capture_module)
    database_tuple = tuple(databases)
    requested_salts = collect_database_salts(database_tuple)
    if not requested_salts:
        raise KeyExtractionError("当前账号未发现可识别的加密数据库")

    script = """
on run argv
    set commandText to quoted form of item 1 of argv & " " & quoted form of item 2 of argv
    return do shell script commandText with administrator privileges
end run
""".strip()
    try:
        result = runner(
            ["/usr/bin/osascript", "-e", script, str(launcher), str(capture_module)],
            check=False,
            capture_output=True,
            text=True,
            timeout=310,
        )
    except subprocess.TimeoutExpired as exc:
        raise KeyExtractionError("密钥提取超时，已停止调试并恢复微信") from exc
    except OSError as exc:
        raise KeyExtractionError("无法启动 LLDB 密钥提取器") from exc

    password_match = _PASSWORD_LINE.search(result.stdout)
    pid_match = _PID_LINE.search(result.stdout)
    if result.returncode != 0 or password_match is None or pid_match is None:
        if "(-128)" in result.stderr or "User canceled" in result.stderr:
            raise KeyExtractionError("用户取消了管理员授权")
        raise KeyExtractionError(
            "未捕获当前账号密钥；请完全退出微信后重试，并在提示后重新登录当前账号"
        )

    password = bytearray.fromhex(password_match.group(1))
    try:
        pairs = derive_verified_key_pairs(bytes(password), database_tuple)
    finally:
        for index in range(len(password)):
            password[index] = 0
    if set(requested_salts) != {pair.salt_hex for pair in pairs}:
        raise KeyExtractionError(
            "当前登录账号与所选账号不匹配；每个微信账号密钥不同，请登录目标账号后重新提取"
        )
    pid = int(pid_match.group(1))
    if pid <= 1:
        raise KeyExtractionError("微信进程 PID 无效")
    return CapturedAccountKeys(wechat_pid=pid, keys=pairs)


def _verified_executable(path: Path) -> Path:
    resolved = _verified_file(path)
    if not os.access(resolved, os.X_OK):
        raise KeyExtractionError("LLDB 启动器不存在或不可执行")
    return resolved


def _verified_file(path: Path) -> Path:
    if path.is_symlink():
        raise KeyExtractionError("LLDB 提取组件无效")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise KeyExtractionError("LLDB 提取组件不存在") from exc
    if not resolved.is_file():
        raise KeyExtractionError("LLDB 提取组件无效")
    return resolved
