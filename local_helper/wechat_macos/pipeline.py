"""微信进程密钥提取、退出等待、快照和解密流水线。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from local_helper.wechat_macos.decryptor import decrypt_database, verify_plain_sqlite
from local_helper.wechat_macos.discovery import WeChatAccount
from local_helper.wechat_macos.key_extractor import (
    CapturedAccountKeys,
    KeyExtractionError,
    WeChatKeyPair,
    extract_current_account_keys,
)
from local_helper.wechat_macos.sip import SIPStatus
from local_helper.wechat_macos.snapshot import create_database_snapshot
from local_helper.workflow import ExpertWorkflow, WorkflowState


class WeChatPipelineError(RuntimeError):
    """流水线失败，错误消息不包含密钥和聊天内容。"""


def find_wechat_pid(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    try:
        result = runner(
            ["/usr/bin/pgrep", "-x", "WeChat"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WeChatPipelineError("无法检查微信进程") from exc
    pids = [
        int(line)
        for line in result.stdout.splitlines()
        if line.strip().isdigit() and int(line) > 1
    ]
    if result.returncode != 0 or not pids:
        raise WeChatPipelineError("请先启动微信并登录需要导出的账号")
    return min(pids)


def wait_for_process_exit(
    pid: int, *, timeout_seconds: int = 300, interval_seconds: float = 1.0
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(interval_seconds)
    return False


def run_expert_decryption(
    *,
    workflow: ExpertWorkflow,
    task_id: str,
    account: WeChatAccount,
    capture_launcher: Path,
    capture_module: Path,
    sqlcipher_binary: Path,
    sip_status: SIPStatus,
    process_waiter: Callable[[int], bool] = wait_for_process_exit,
) -> WorkflowState:
    captured: CapturedAccountKeys | None = None

    def extract() -> tuple[WeChatKeyPair, ...]:
        nonlocal captured
        captured = extract_current_account_keys(
            launcher=capture_launcher,
            capture_module=capture_module,
            databases=account.databases,
            sip_status=sip_status,
        )
        return captured.keys

    def snapshot_and_decrypt(
        keys: tuple[WeChatKeyPair, ...], task_dir: Path
    ) -> tuple[Path, ...]:
        return decrypt_account_databases(
            account=account,
            task_dir=task_dir,
            keys=keys,
            sqlcipher_binary=sqlcipher_binary,
        )

    return workflow.decrypt_while_sip_disabled(
        task_id=task_id,
        extract_keys=extract,
        wait_for_wechat_exit=lambda: (
            captured is not None and process_waiter(captured.wechat_pid)
        ),
        snapshot_and_decrypt=snapshot_and_decrypt,
    )


def decrypt_account_databases(
    *,
    account: WeChatAccount,
    task_dir: Path,
    keys: tuple[WeChatKeyPair, ...],
    sqlcipher_binary: Path,
) -> tuple[Path, ...]:
    keys_by_salt: dict[str, list[WeChatKeyPair]] = {}
    for pair in keys:
        keys_by_salt.setdefault(pair.salt_hex, []).append(pair)
    encrypted_root = task_dir / "encrypted"
    plain_root = task_dir / "plain"
    encrypted_root.mkdir(parents=True, mode=0o700)
    plain_root.mkdir(parents=True, mode=0o700)
    outputs: list[Path] = []

    for source in account.databases:
        relative = source.relative_to(account.db_storage)
        snapshot_dir = encrypted_root / relative.parent
        snapshot = create_database_snapshot(source, snapshot_dir)[0].snapshot
        output = plain_root / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        with snapshot.open("rb") as stream:
            header = stream.read(16)
        if header == b"SQLite format 3\x00":
            shutil.copyfile(snapshot, output)
            verify_plain_sqlite(output)
        else:
            candidates = keys_by_salt.get(header.hex(), ())
            if not candidates:
                raise KeyExtractionError(
                    f"数据库 {relative.as_posix()} 缺少可验证的密钥"
                )
            last_error: Exception | None = None
            for pair in candidates:
                try:
                    decrypt_database(
                        encrypted_db=snapshot,
                        output_db=output,
                        key_pair=pair,
                        sqlcipher_binary=sqlcipher_binary,
                    )
                    break
                except Exception as exc:
                    last_error = exc
            else:
                raise KeyExtractionError(
                    f"数据库 {relative.as_posix()} 的密钥候选均未通过验证"
                ) from last_error
        outputs.append(output)
    return tuple(outputs)
