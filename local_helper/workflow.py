"""专家模式的可恢复状态机，不持久化数据库密钥。"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from core.file_utils import atomic_write_json
from local_helper.wechat_macos.sip import SIPStatus, get_sip_status


class WorkflowError(RuntimeError):
    """专家模式的安全前置条件不满足。"""


class WorkflowPhase(str, Enum):
    AWAITING_SIP_DISABLED = "awaiting_sip_disabled"
    EXTRACTING_KEYS = "extracting_keys"
    AWAITING_WECHAT_EXIT = "awaiting_wechat_exit"
    DECRYPTING = "decrypting"
    AWAITING_SIP_ENABLED = "awaiting_sip_enabled"
    READY_TO_EXPORT = "ready_to_export"
    EXPORTING = "exporting"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkflowState:
    task_id: str
    account_id: str
    account_root: str
    phase: WorkflowPhase
    decrypted_files: tuple[str, ...] = ()
    output_dir: str = ""
    error_code: str = ""
    error_detail: str = ""


KeyCollection = TypeVar("KeyCollection")


class ExpertWorkflow:
    def __init__(
        self,
        root: Path,
        *,
        sip_status: Callable[[], SIPStatus] = get_sip_status,
        on_state_change: Callable[[WorkflowState], None] | None = None,
    ):
        if root.exists() and root.is_symlink():
            raise ValueError("任务目录不能是符号链接")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve()
        os.chmod(self.root, 0o700)
        self._sip_status = sip_status
        self._on_state_change = on_state_change
        self._lock = threading.RLock()

    def prepare(
        self, *, task_id: str, account_id: str, account_root: Path
    ) -> WorkflowState:
        if self._sip_status() is not SIPStatus.ENABLED:
            raise WorkflowError("开始专家模式前必须保持开启 SIP")
        task_dir = self._task_dir(task_id)
        if task_dir.exists():
            raise WorkflowError("任务已经存在")
        if account_root.is_symlink():
            raise WorkflowError("微信账号目录不能是符号链接")
        resolved_account = account_root.resolve(strict=True)
        if not resolved_account.is_dir():
            raise WorkflowError("微信账号目录无效")
        task_dir.mkdir(mode=0o700)
        state = WorkflowState(
            task_id=task_id,
            account_id=account_id,
            account_root=str(resolved_account),
            phase=WorkflowPhase.AWAITING_SIP_DISABLED,
        )
        return self._save(state)

    def decrypt_while_sip_disabled(
        self,
        *,
        task_id: str,
        extract_keys: Callable[[], KeyCollection],
        wait_for_wechat_exit: Callable[[], bool],
        snapshot_and_decrypt: Callable[[KeyCollection, Path], Iterable[Path]],
    ) -> WorkflowState:
        with self._lock:
            state = self.load(task_id)
            if state.phase is not WorkflowPhase.AWAITING_SIP_DISABLED:
                raise WorkflowError("任务当前不能执行解密")
            if self._sip_status() is not SIPStatus.DISABLED:
                raise WorkflowError("请先在恢复模式中手动关闭 SIP")

            keys: KeyCollection | None = None
            try:
                self._save(self._replace(state, phase=WorkflowPhase.EXTRACTING_KEYS))
                keys = extract_keys()
                if not keys:
                    raise WorkflowError("未取得可验证的数据库密钥")
                self._save(
                    self._replace(state, phase=WorkflowPhase.AWAITING_WECHAT_EXIT)
                )
                if not wait_for_wechat_exit():
                    raise WorkflowError("取得密钥后必须先完全退出微信")
                self._save(self._replace(state, phase=WorkflowPhase.DECRYPTING))
                task_dir = self._task_dir(task_id)
                outputs = tuple(snapshot_and_decrypt(keys, task_dir))
                relative_outputs = self._validate_outputs(task_dir, outputs)
                return self._save(
                    self._replace(
                        state,
                        phase=WorkflowPhase.AWAITING_SIP_ENABLED,
                        decrypted_files=relative_outputs,
                    )
                )
            except Exception:
                self._clear_decryption_outputs(task_id)
                self._save(
                    self._replace(
                        state,
                        phase=WorkflowPhase.FAILED,
                        error_code="decryption_failed",
                    )
                )
                raise
            finally:
                keys = None

    def authorize_export(self, task_id: str) -> WorkflowState:
        with self._lock:
            state = self.load(task_id)
            if state.phase is not WorkflowPhase.AWAITING_SIP_ENABLED:
                raise WorkflowError("解密快照尚未准备好")
            if self._sip_status() is not SIPStatus.ENABLED:
                raise WorkflowError("完整导出前必须重新开启 SIP 并重启 macOS")
            self._validate_outputs(self._task_dir(task_id), self.decrypted_paths(state))
            return self._save(self._replace(state, phase=WorkflowPhase.READY_TO_EXPORT))

    def start_export(self, task_id: str) -> WorkflowState:
        with self._lock:
            state = self.load(task_id)
            if state.phase is not WorkflowPhase.READY_TO_EXPORT:
                raise WorkflowError("解密快照尚未准备好，不能开始导出")
            if self._sip_status() is not SIPStatus.ENABLED:
                raise WorkflowError("SIP 未开启，拒绝导出")
            return self._save(self._replace(state, phase=WorkflowPhase.EXPORTING))

    def finish_export(
        self, task_id: str, *, output_dir: Path, partial: bool = False
    ) -> WorkflowState:
        with self._lock:
            state = self.load(task_id)
            if state.phase is not WorkflowPhase.EXPORTING:
                raise WorkflowError("任务当前不在导出阶段")
            phase = WorkflowPhase.PARTIAL if partial else WorkflowPhase.COMPLETE
            resolved_output = output_dir.resolve(strict=True)
            if not resolved_output.is_dir():
                raise WorkflowError("导出目录不存在")
            return self._save(
                self._replace(state, phase=phase, output_dir=str(resolved_output))
            )

    def fail(
        self, task_id: str, *, error_code: str, error_detail: str = ""
    ) -> WorkflowState:
        if not re.fullmatch(r"[a-z0-9_]{1,64}", error_code):
            raise ValueError("错误代码格式无效")
        with self._lock:
            state = self.load(task_id)
            return self._save(
                self._replace(
                    state,
                    phase=WorkflowPhase.FAILED,
                    error_code=error_code,
                    error_detail=error_detail,
                )
            )

    def delete_task(self, task_id: str) -> None:
        """仅在非活动阶段删除任务、解密快照和状态。"""
        with self._lock:
            state = self.load(task_id)
            if state.phase in {
                WorkflowPhase.EXTRACTING_KEYS,
                WorkflowPhase.AWAITING_WECHAT_EXIT,
                WorkflowPhase.DECRYPTING,
                WorkflowPhase.EXPORTING,
            }:
                raise WorkflowError(
                    "任务正在处理，不能删除；请等待当前步骤完成或超时清理"
                )
            task_dir = self._task_dir(task_id)
            if task_dir.is_symlink() or not task_dir.resolve().is_relative_to(
                self.root
            ):
                raise WorkflowError("任务目录无效")
            shutil.rmtree(task_dir)

    def load(self, task_id: str) -> WorkflowState:
        state_file = self._task_dir(task_id) / "state.json"
        if state_file.is_symlink():
            raise WorkflowError("任务状态文件无效")
        try:
            raw = json.loads(state_file.read_text(encoding="utf-8"))
            state = WorkflowState(
                task_id=raw["task_id"],
                account_id=raw["account_id"],
                account_root=raw["account_root"],
                phase=WorkflowPhase(raw["phase"]),
                decrypted_files=tuple(raw.get("decrypted_files", ())),
                output_dir=raw.get("output_dir", ""),
                error_code=raw.get("error_code", ""),
                error_detail=raw.get("error_detail", ""),
            )
            if state.task_id != task_id:
                raise WorkflowError("任务状态标识不匹配")
            return state
        except (
            FileNotFoundError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise WorkflowError("任务状态不存在或已损坏") from exc

    def decrypted_paths(self, state: WorkflowState) -> tuple[Path, ...]:
        task_dir = self._task_dir(state.task_id)
        return tuple(task_dir / relative for relative in state.decrypted_files)

    def list_states(self) -> tuple[WorkflowState, ...]:
        states: list[WorkflowState] = []
        for task_dir in sorted(
            self.root.iterdir(), key=lambda path: path.stat().st_mtime, reverse=True
        ):
            if (
                not task_dir.is_dir()
                or task_dir.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{32}", task_dir.name)
            ):
                continue
            try:
                states.append(self.load(task_dir.name))
            except WorkflowError:
                continue
        return tuple(states)

    def _task_dir(self, task_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError("任务 ID 格式无效")
        return self.root / task_id

    def _save(self, state: WorkflowState) -> WorkflowState:
        task_dir = self._task_dir(state.task_id)
        task_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_file = task_dir / "state.json"
        payload = asdict(state)
        payload["phase"] = state.phase.value
        atomic_write_json(state_file, payload)
        os.chmod(state_file, 0o600)
        if self._on_state_change:
            self._on_state_change(state)
        return state

    def _validate_outputs(
        self, task_dir: Path, outputs: Iterable[Path]
    ) -> tuple[str, ...]:
        relative: list[str] = []
        for output in outputs:
            if output.is_symlink():
                raise WorkflowError("解密快照不能是符号链接")
            try:
                resolved = output.resolve(strict=True)
            except OSError as exc:
                raise WorkflowError("解密快照不存在") from exc
            if not resolved.is_file() or not resolved.is_relative_to(task_dir):
                raise WorkflowError("解密快照必须位于私有任务目录")
            relative.append(resolved.relative_to(task_dir).as_posix())
        if not relative:
            raise WorkflowError("没有生成可验证的解密快照")
        return tuple(relative)

    def _clear_decryption_outputs(self, task_id: str) -> None:
        task_dir = self._task_dir(task_id)
        for name in ("encrypted", "plain"):
            path = task_dir / name
            if path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)

    @staticmethod
    def _replace(state: WorkflowState, **changes) -> WorkflowState:
        values = asdict(state)
        values.update(changes)
        values["phase"] = WorkflowPhase(values["phase"])
        values["decrypted_files"] = tuple(values["decrypted_files"])
        return WorkflowState(**values)
