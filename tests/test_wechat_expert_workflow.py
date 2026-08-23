import json
from pathlib import Path

import pytest

from local_helper.wechat_macos.sip import SIPStatus
from local_helper.workflow import ExpertWorkflow, WorkflowError, WorkflowPhase


def _workflow(tmp_path: Path, status: list[SIPStatus]) -> ExpertWorkflow:
    return ExpertWorkflow(tmp_path / "tasks", sip_status=lambda: status[-1])


def test_prepare_requires_enabled_sip(tmp_path: Path):
    workflow = _workflow(tmp_path, [SIPStatus.DISABLED])

    with pytest.raises(WorkflowError, match="开启 SIP"):
        workflow.prepare(task_id="a" * 32, account_id="account", account_root=tmp_path)


def test_decryption_keeps_keys_in_memory_and_requires_wechat_exit(tmp_path: Path):
    status = [SIPStatus.ENABLED]
    workflow = _workflow(tmp_path, status)
    workflow.prepare(task_id="a" * 32, account_id="account", account_root=tmp_path)
    status.append(SIPStatus.DISABLED)
    key = "super-secret-key"

    with pytest.raises(WorkflowError, match="退出微信"):
        workflow.decrypt_while_sip_disabled(
            task_id="a" * 32,
            extract_keys=lambda: (key,),
            wait_for_wechat_exit=lambda: False,
            snapshot_and_decrypt=lambda _keys, _directory: (),
        )

    state_file = tmp_path / "tasks" / ("a" * 32) / "state.json"
    assert key not in state_file.read_text(encoding="utf-8")
    assert workflow.load("a" * 32).phase is WorkflowPhase.FAILED


def test_failed_decryption_removes_incomplete_snapshots(tmp_path: Path):
    status = [SIPStatus.ENABLED]
    workflow = _workflow(tmp_path, status)
    task_id = "9" * 32
    workflow.prepare(task_id=task_id, account_id="account", account_root=tmp_path)
    status.append(SIPStatus.DISABLED)

    def fail_after_partial_output(_keys, task_dir):
        (task_dir / "encrypted").mkdir()
        (task_dir / "encrypted" / "message.db").write_bytes(b"encrypted")
        (task_dir / "plain").mkdir()
        (task_dir / "plain" / "message.db").write_bytes(b"partial")
        raise RuntimeError("decrypt failed")

    with pytest.raises(RuntimeError, match="decrypt failed"):
        workflow.decrypt_while_sip_disabled(
            task_id=task_id,
            extract_keys=lambda: ("memory-only",),
            wait_for_wechat_exit=lambda: True,
            snapshot_and_decrypt=fail_after_partial_output,
        )

    assert not (tmp_path / "tasks" / task_id / "encrypted").exists()
    assert not (tmp_path / "tasks" / task_id / "plain").exists()
    assert workflow.load(task_id).phase is WorkflowPhase.FAILED


def test_complete_decryption_waits_for_sip_reenable(tmp_path: Path):
    status = [SIPStatus.ENABLED]
    workflow = _workflow(tmp_path, status)
    task_id = "b" * 32
    workflow.prepare(task_id=task_id, account_id="account", account_root=tmp_path)
    status.append(SIPStatus.DISABLED)

    def decrypt(keys, directory):
        assert keys == ("memory-only",)
        output = directory / "plain" / "message.db"
        output.parent.mkdir()
        output.write_bytes(b"SQLite format 3\0")
        return (output,)

    state = workflow.decrypt_while_sip_disabled(
        task_id=task_id,
        extract_keys=lambda: ("memory-only",),
        wait_for_wechat_exit=lambda: True,
        snapshot_and_decrypt=decrypt,
    )

    assert state.phase is WorkflowPhase.AWAITING_SIP_ENABLED
    persisted = json.loads((tmp_path / "tasks" / task_id / "state.json").read_text(encoding="utf-8"))
    assert "memory-only" not in json.dumps(persisted)
    with pytest.raises(WorkflowError, match="重新开启 SIP"):
        workflow.authorize_export(task_id)

    status.append(SIPStatus.ENABLED)
    assert workflow.authorize_export(task_id).phase is WorkflowPhase.READY_TO_EXPORT


def test_export_cannot_start_before_decryption(tmp_path: Path):
    workflow = _workflow(tmp_path, [SIPStatus.ENABLED])
    task_id = "c" * 32
    workflow.prepare(task_id=task_id, account_id="account", account_root=tmp_path)

    with pytest.raises(WorkflowError, match="尚未准备好"):
        workflow.start_export(task_id)


def test_fail_records_error_detail(tmp_path: Path):
    workflow = _workflow(tmp_path, [SIPStatus.ENABLED])
    task_id = "d" * 32
    workflow.prepare(task_id=task_id, account_id="account", account_root=tmp_path)

    workflow.fail(task_id, error_code="decryption_failed", error_detail="未能从微信进程取得可验证的密钥候选")

    state = workflow.load(task_id)
    assert state.phase is WorkflowPhase.FAILED
    assert state.error_detail == "未能从微信进程取得可验证的密钥候选"


def test_delete_task_removes_private_snapshots_only_in_safe_phase(tmp_path: Path):
    workflow = _workflow(tmp_path, [SIPStatus.ENABLED])
    task_id = "e" * 32
    workflow.prepare(task_id=task_id, account_id="account", account_root=tmp_path)
    private_file = tmp_path / "tasks" / task_id / "plain" / "message.db"
    private_file.parent.mkdir()
    private_file.write_bytes(b"private")

    workflow.delete_task(task_id)

    assert not (tmp_path / "tasks" / task_id).exists()


def test_delete_task_rejects_active_phase(tmp_path: Path):
    status = [SIPStatus.ENABLED]
    workflow = _workflow(tmp_path, status)
    task_id = "f" * 32
    state = workflow.prepare(task_id=task_id, account_id="account", account_root=tmp_path)
    workflow._save(workflow._replace(state, phase=WorkflowPhase.DECRYPTING))

    with pytest.raises(WorkflowError, match="正在处理"):
        workflow.delete_task(task_id)
