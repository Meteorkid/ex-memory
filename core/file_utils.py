"""文件操作工具：原子写入、带锁 JSON 读写。"""

import json
import os
import tempfile
import fcntl
import time
import logging
from pathlib import Path
from typing import Any, Callable, Union

logger = logging.getLogger("ex-memory")

LOCK_TIMEOUT = 5  # 文件锁超时秒数


def atomic_write(path: Path, content: str, encoding: str = "utf-8"):
    """原子写入：先写临时文件，再 os.replace。"""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding=encoding, suffix=".tmp",
        dir=path.parent, delete=False,
    )
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, str(path))
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any, encoding: str = "utf-8"):
    """原子写入 JSON 文件。"""
    content = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write(path, content, encoding=encoding)


def locked_read(path: Path, encoding: str = "utf-8") -> str:
    """带文件锁读取文本文件。"""
    if not path.exists():
        raise FileNotFoundError(path)
    with open(path, "r", encoding=encoding) as f:
        _lock(f, fcntl.LOCK_SH)
        return f.read()


def locked_write(path: Path, content: str, encoding: str = "utf-8"):
    """带文件锁写入文本文件，结合原子替换。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        _lock(lock_file, fcntl.LOCK_EX)
        atomic_write(path, content, encoding=encoding)


def locked_read_json(path: Path) -> Any:
    """带文件锁读取 JSON 文件。"""
    content = locked_read(path)
    return json.loads(content)


def locked_write_json(path: Path, data: Any):
    """带文件锁原子写入 JSON 文件。"""
    content = json.dumps(data, ensure_ascii=False, indent=2)
    locked_write(path, content)


def locked_update_json(path: Path, default: Union[Any, Callable[[], Any]], updater: Callable[[Any], Any]) -> Any:
    """在同一把文件锁内完成 JSON 读-改-写，返回 updater 的结果。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        _lock(lock_file, fcntl.LOCK_EX)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = default() if callable(default) else default
        result = updater(data)
        atomic_write_json(path, data)
        return result


def _lock(f, mode: int):
    """阻塞获取文件锁，带有超时重试。"""
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise TimeoutError(f"无法获取文件锁: {f.name}")
            time.sleep(0.05)
