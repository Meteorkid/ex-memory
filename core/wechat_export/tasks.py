"""WechatExporter 后台任务管理。"""

import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import config
from core.exporters import wechat_adapter
from core.file_utils import locked_read_json, locked_update_json, locked_write_json
from core.path_safety import resolve_under
from core.wechat_export.backups import get_backup_path

TASK_STATUSES = {"pending", "running", "success", "failed"}


def create_export_task(
    backup_id: str,
    account: str,
    sessions: tuple[str, ...] = (),
    async_loading: str = "onscroll",
    enable_filter: bool = False,
) -> dict:
    """创建并启动一个微信导出任务。"""
    account = account.strip()
    if not account:
        raise ValueError("微信账号不能为空")
    backup_dir = get_backup_path(backup_id)
    wechat_adapter.resolve_wechat_exporter_binary()

    task_id = uuid.uuid4().hex
    output_dir = _task_dir(task_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    now = _now()
    task = {
        "task_id": task_id,
        "status": "pending",
        "backup_id": backup_id,
        "backup_dir": str(backup_dir),
        "output_dir": str(output_dir),
        "account": account,
        "sessions": list(sessions),
        "async_loading": async_loading,
        "enable_filter": enable_filter,
        "created_at": now,
        "updated_at": now,
        "started_at": "",
        "finished_at": "",
        "stdout": "",
        "stderr": "",
        "error": "",
    }
    locked_write_json(_task_file(task_id), task)
    thread = threading.Thread(
        target=_run_task,
        args=(task_id, backup_dir, output_dir, account, sessions, async_loading, enable_filter),
        daemon=True,
    )
    thread.start()
    return get_task(task_id)


def get_task(task_id: str) -> dict:
    """读取任务状态，并附加输出文件列表。"""
    task = locked_read_json(_task_file(task_id))
    task["output_files"] = list_output_files(task_id)
    return task


def list_output_files(task_id: str) -> list[dict]:
    task_dir = _task_dir(task_id)
    if not task_dir.exists():
        return []
    files = []
    for path in sorted(task_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name == "task.json" or path.name.endswith((".lock", ".tmp")):
            continue
        rel = path.relative_to(task_dir).as_posix()
        files.append({
            "path": rel,
            "name": path.name,
            "size": path.stat().st_size,
            "download_url": f"/api/wechat-export/tasks/{task_id}/files/{quote(rel, safe='/')}",
        })
    return files


def get_output_file(task_id: str, rel_path: str) -> Path:
    task_dir = _task_dir(task_id)
    path = resolve_under(task_dir, rel_path)
    if path.name == "task.json" or path.is_symlink() or not path.is_file():
        raise FileNotFoundError("输出文件不存在")
    return path


def _run_task(
    task_id: str,
    backup_dir: Path,
    output_dir: Path,
    account: str,
    sessions: tuple[str, ...],
    async_loading: str,
    enable_filter: bool,
):
    _update_task(task_id, status="running", started_at=_now())
    try:
        result = wechat_adapter.run_wechat_exporter(wechat_adapter.WechatExportOptions(
            backup_dir=backup_dir,
            output_dir=output_dir,
            account=account,
            sessions=sessions,
            async_loading=async_loading,
            enable_filter=enable_filter,
        ))
        _update_task(
            task_id,
            status="success",
            finished_at=_now(),
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
    except subprocess.CalledProcessError as e:
        _update_task(
            task_id,
            status="failed",
            finished_at=_now(),
            stdout=e.stdout or "",
            stderr=e.stderr or "",
            error=str(e),
        )
    except Exception as e:
        _update_task(task_id, status="failed", finished_at=_now(), error=str(e))


def _update_task(task_id: str, **fields):
    def updater(data: dict):
        data.update(fields)
        data["updated_at"] = _now()
        return data

    locked_update_json(_task_file(task_id), {}, updater)


def _task_file(task_id: str) -> Path:
    return _task_dir(task_id) / "task.json"


def _task_dir(task_id: str) -> Path:
    if not task_id or any(c not in "0123456789abcdef" for c in task_id) or len(task_id) != 32:
        raise ValueError("任务 ID 格式无效")
    root = config.WECHAT_EXPORT_OUTPUT_DIR.expanduser()
    return resolve_under(root, task_id)


def _now() -> str:
    return datetime.now().isoformat()
