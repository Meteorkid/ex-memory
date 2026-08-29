"""macOS 微信 4.x 账号和数据库发现。"""

from __future__ import annotations

import hashlib
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_WECHAT_APP = Path("/Applications/WeChat.app")
SUPPORTED_WECHAT_VERSIONS = ("4.1.12",)
DEFAULT_XWECHAT_FILES = (
    Path.home()
    / "Library"
    / "Containers"
    / "com.tencent.xinWeChat"
    / "Data"
    / "Documents"
    / "xwechat_files"
)


@dataclass(frozen=True)
class WeChatAccount:
    account_id: str
    root: Path
    db_storage: Path
    databases: tuple[Path, ...]
    schema_fingerprint: str

    @property
    def owner_wxid(self) -> str:
        prefix, separator, suffix = self.account_id.rpartition("_")
        if (
            separator
            and prefix.startswith("wxid_")
            and len(suffix) == 4
            and all(char in "0123456789abcdef" for char in suffix.lower())
        ):
            return prefix
        return self.account_id


@dataclass(frozen=True)
class WeChatEnvironment:
    app_version: str
    accounts: tuple[WeChatAccount, ...]
    data_root_exists: bool
    data_accessible: bool = True
    error_code: str = ""
    current_account_id: str = ""


def detect_environment(
    *,
    app_path: Path = DEFAULT_WECHAT_APP,
    data_root: Path = DEFAULT_XWECHAT_FILES,
    process_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> WeChatEnvironment:
    data_root_exists = data_root.exists() and data_root.is_dir()
    try:
        accounts = discover_accounts(data_root)
        current_account_id = detect_current_account(accounts, runner=process_runner)
        data_accessible = True
        error_code = ""
    except PermissionError:
        accounts = ()
        current_account_id = ""
        data_accessible = False
        error_code = "full_disk_access_required"
    return WeChatEnvironment(
        app_version=read_wechat_version(app_path),
        accounts=accounts,
        data_root_exists=data_root_exists,
        data_accessible=data_accessible,
        error_code=error_code,
        current_account_id=current_account_id,
    )


def detect_current_account(
    accounts: tuple[WeChatAccount, ...],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> str:
    """仅在一个账号有数据库被微信主进程打开时返回它。"""
    if not accounts:
        return ""
    try:
        process_result = runner(
            ["/usr/bin/pgrep", "-x", "WeChat"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if process_result.returncode != 0:
        return ""
    pids = [
        line
        for line in process_result.stdout.splitlines()
        if line.isdigit() and int(line) > 1
    ]
    open_database_counts = {account.account_id: 0 for account in accounts}
    for pid in pids:
        try:
            files_result = runner(
                ["/usr/sbin/lsof", "-n", "-p", pid, "-Fn"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in files_result.stdout.splitlines():
            if not line.startswith("n"):
                continue
            open_path = Path(line[1:])
            for account in accounts:
                try:
                    relative = open_path.relative_to(account.db_storage)
                except ValueError:
                    continue
                if relative.parts:
                    open_database_counts[account.account_id] += 1
                break
    active_accounts = [
        account_id for account_id, count in open_database_counts.items() if count > 0
    ]
    return active_accounts[0] if len(active_accounts) == 1 else ""


def read_wechat_version(app_path: Path = DEFAULT_WECHAT_APP) -> str:
    info_plist = app_path / "Contents" / "Info.plist"
    try:
        with info_plist.open("rb") as stream:
            info = plistlib.load(stream)
    except (FileNotFoundError, PermissionError, plistlib.InvalidFileException):
        return ""
    return str(
        info.get("CFBundleShortVersionString") or info.get("CFBundleVersion") or ""
    )


def discover_accounts(
    data_root: Path = DEFAULT_XWECHAT_FILES,
) -> tuple[WeChatAccount, ...]:
    if not data_root.exists() or not data_root.is_dir():
        return ()
    root_resolved = data_root.resolve()
    accounts: list[WeChatAccount] = []
    for account_root in sorted(data_root.iterdir(), key=lambda path: path.name):
        if not account_root.is_dir() or account_root.is_symlink():
            continue
        resolved = account_root.resolve()
        if not resolved.is_relative_to(root_resolved):
            continue
        db_storage = resolved / "db_storage"
        if not db_storage.is_dir() or db_storage.is_symlink():
            continue
        databases = tuple(
            path
            for path in sorted(db_storage.rglob("*.db"))
            if path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(db_storage.resolve())
        )
        if not databases:
            continue
        accounts.append(
            WeChatAccount(
                account_id=account_root.name,
                root=resolved,
                db_storage=db_storage,
                databases=databases,
                schema_fingerprint=_database_fingerprint(db_storage, databases),
            )
        )
    return tuple(accounts)


def _database_fingerprint(db_storage: Path, databases: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in databases:
        digest.update(path.relative_to(db_storage).as_posix().encode("utf-8"))
        digest.update(path.stat().st_size.to_bytes(8, "big", signed=False))
        with path.open("rb") as stream:
            digest.update(stream.read(16))
    return digest.hexdigest()
