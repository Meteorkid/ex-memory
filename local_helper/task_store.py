"""仅暴露无隐私字段的本地任务状态。"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass


PUBLIC_STATUSES = frozenset({"awaiting_local_confirmation", "running", "success", "partial", "failed", "cancelled"})


@dataclass
class PublicTaskStatus:
    task_id: str
    status: str = "awaiting_local_confirmation"
    phase: str = "launch"
    progress: int = 0
    error_code: str = ""

    def public_dict(self) -> dict:
        return asdict(self)


class PublicTaskStore:
    def __init__(self):
        self._tasks: dict[str, PublicTaskStatus] = {}
        self._lock = threading.Lock()

    def create(self) -> PublicTaskStatus:
        task = PublicTaskStatus(task_id=uuid.uuid4().hex)
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> PublicTaskStatus:
        if len(task_id) != 32 or any(char not in "0123456789abcdef" for char in task_id):
            raise ValueError("任务 ID 格式无效")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            return PublicTaskStatus(**task.public_dict())

    def update(self, task_id: str, *, status: str, phase: str, progress: int, error_code: str = "") -> None:
        if status not in PUBLIC_STATUSES:
            raise ValueError("任务状态无效")
        if not 0 <= progress <= 100:
            raise ValueError("任务进度必须介于 0 和 100")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            task.status = status
            task.phase = phase
            task.progress = progress
            task.error_code = error_code
