"""异步任务：提交、执行、状态查询。

导入、反思、朋友圈生成、备份原本在 HTTP 请求内同步执行。导入尤其严重——
它是 async 路由里的阻塞调用，一个用户导入会冻住整个 worker 的事件循环。

**执行模型**：进程内线程池 worker。这是一个刻意的折中：
- 好处是单容器部署即可运行，不需要额外的 worker 进程与部署拓扑改动；
- 代价是任务不跨副本调度，且进程重启会留下「running 却无人执行」的僵尸。
  后者靠 heartbeat 识别并在启动时回收（reclaim_stale）。

任务状态落库而非放内存：进程重启后用户要能看到「那个导入到底成没成」。
后续要换成独立 worker 进程时，替换 _dispatch 即可，接口不变。
"""

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from core.observability import observe_task

logger = logging.getLogger("ex-memory")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

# 心跳超过这个时长仍处于 running 的任务，视为进程崩溃遗留
STALE_AFTER_SECONDS = 900

_handlers: dict[str, Callable[..., Any]] = {}
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


class TaskProgress:
    """交给任务处理器用来汇报进度。"""

    def __init__(self, task_id: str):
        self.task_id = task_id

    def update(self, percent: int, detail: str = "") -> None:
        _update_task(
            self.task_id, progress=max(0, min(100, int(percent))), detail=detail or None
        )


def register(task_type: str) -> Callable:
    """注册任务处理器。处理器签名为 (progress, **payload)。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        _handlers[task_type] = func
        return func

    return decorator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _executor_instance() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            import config

            _executor = ThreadPoolExecutor(
                max_workers=config.TASK_WORKER_THREADS, thread_name_prefix="task"
            )
        return _executor


def enqueue(
    task_type: str, user_id: int, payload: dict, slug: Optional[str] = None
) -> str:
    """提交任务，立即返回 task_id。"""
    if task_type not in _handlers:
        raise ValueError(f"未注册的任务类型: {task_type}")

    from server.auth import _get_conn

    task_id = uuid.uuid4().hex
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tasks (id, user_id, slug, task_type, status, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                user_id,
                slug,
                task_type,
                STATUS_QUEUED,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()

    _executor_instance().submit(_run, task_id)
    return task_id


def _run(task_id: str) -> None:
    task = get_task(task_id)
    if task is None or task["status"] != STATUS_QUEUED:
        return

    handler = _handlers.get(task["task_type"])
    if handler is None:
        _update_task(
            task_id,
            status=STATUS_FAILED,
            error=f"未注册的任务类型: {task['task_type']}",
        )
        return

    _update_task(
        task_id,
        status=STATUS_RUNNING,
        started_at=_now(),
        heartbeat_at=_now(),
        attempts=int(task["attempts"]) + 1,
    )
    payload = json.loads(task["payload"] or "{}")
    try:
        result = handler(TaskProgress(task_id), **payload)
        observe_task(task["task_type"], STATUS_SUCCEEDED)
        _update_task(
            task_id,
            status=STATUS_SUCCEEDED,
            progress=100,
            result=json.dumps(result, ensure_ascii=False) if result else None,
            finished_at=_now(),
        )
    except Exception as e:  # noqa: BLE001 — 任务失败要落库，不能只打日志
        logger.error(
            "任务失败 id=%s type=%s: %s", task_id, task["task_type"], e, exc_info=True
        )
        observe_task(task["task_type"], STATUS_FAILED)
        _update_task(
            task_id,
            status=STATUS_FAILED,
            error=str(e)[:500],
            finished_at=_now(),
        )


def _update_task(task_id: str, **fields: Any) -> None:
    if not fields:
        return
    from server.auth import _get_conn

    fields.setdefault("heartbeat_at", _now())
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with _get_conn() as conn:
        conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*fields.values(), task_id),
        )
        conn.commit()


def get_task(task_id: str, user_id: Optional[int] = None) -> Optional[dict]:
    """取任务。传 user_id 时同时做归属校验。"""
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task = dict(row)
    if user_id is not None and int(task["user_id"]) != user_id:
        return None  # 不区分「不存在」与「不属于你」，避免任务 ID 存在性泄漏
    return task


def list_tasks(user_id: int, limit: int = 20) -> list[dict]:
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def retry(task_id: str, user_id: int) -> bool:
    """重试失败任务。只有失败态可重试，避免把正在跑的任务再跑一遍。"""
    task = get_task(task_id, user_id)
    if task is None or task["status"] != STATUS_FAILED:
        return False
    _update_task(task_id, status=STATUS_QUEUED, error=None, progress=0, detail=None)
    _executor_instance().submit(_run, task_id)
    return True


def reclaim_stale() -> int:
    """把进程崩溃遗留的僵尸任务标记为失败，返回回收数量。

    在启动时调用。不自动重跑：崩溃原因未知，盲目重跑可能反复触发同一问题，
    交给用户决定是否重试更安全。
    """
    from server.auth import _get_conn

    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_SECONDS)
    ).isoformat()
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            UPDATE tasks SET status = ?, error = ?, finished_at = ?
            WHERE status = ? AND (heartbeat_at IS NULL OR heartbeat_at < ?)
            """,
            (
                STATUS_FAILED,
                "任务因服务重启中断，可重试",
                _now(),
                STATUS_RUNNING,
                cutoff,
            ),
        )
        conn.commit()
        count = cursor.rowcount
    if count:
        logger.warning("回收了 %d 个因重启中断的任务", count)
    return count


def shutdown(wait: bool = False) -> None:
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None


def reset_for_tests() -> None:
    shutdown(wait=True)


@register("billing_reconcile")
def _reconcile_billing(progress, external_orders: list, include_subscriptions: bool = False) -> dict:
    """充值侧对账任务（NFR-006）。外部下单/入账后由调度触发。

    payload.external_orders 形如 [{"payment_ref" ..., "amount_micros" ...}]，
    require_topup 等渠道账单解析后投喂。返回充值对账报告；如需同时核对订阅侧，
    置 include_subscriptions=True。
    """
    from core.topup import reconcile_topup

    report = reconcile_topup(external_orders or [])
    if include_subscriptions:
        from core.payments import reconcile

        report["subscriptions"] = reconcile(external_orders or [])
    progress.update(100, "充值对账完成")
    return report
