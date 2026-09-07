"""镜像运行时文件访问门面（FR-034 前置）。

镜像目录内的人格文件（SKILL.md / persona.md / memory.md / corrections /
meta.json / conversations / sessions / corpus.jsonl ...）此前在约 29 个模块里
有 74 处直接走文件系统。它们是多副本运行时无法共享状态、以及后续无法平滑切到
对象存储的根因。

这里提供一层 MirrorStore 门面，把镜像内的**文件级**访问收敛到统一入口：

- 相对路径访问：所有方法以镜像根为基准的相对子路径（rel）定位，由后端统一的
   `resolve_under` 防路径越界，替代各调用处零散的 `Path` 拼接。
- 后端可选择：`LocalMirrorBackend`（默认，文件系统，行为零变化）；对象存储
   后端（S3）是预留位点，本轮不实现（PRD 定为后续独立改动）。

**不做**的：镜像层面的目录遍历 / 整目录操作（版本备份复制、chroma_db 目录树、
快照恢复、镜像删除）。这些点通过 `MirrorStore.path(rel)` 取得安全 Path 后自行
处理，门面不抽象——只统一入口、不改变语义。
"""

import fcntl
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import core.file_utils as file_utils
import core.path_safety as path_safety

logger = logging.getLogger("ex-memory")

LOCK_TIMEOUT = 5  # 文件锁超时秒数


class MirrorBackend(Protocol):
    """镜像文件访问后端协议。

    rel 一律是以镜像根为基准的相对子路径（如 "meta.json"、"sessions/xxx.md"）。
    """

    name: str

    def read_text(self, rel: str, encoding: str = "utf-8") -> str: ...
    def read_bytes(self, rel: str) -> bytes: ...
    def write_text(self, rel: str, content: str, encoding: str = "utf-8") -> None: ...
    def write_bytes(self, rel: str, data: bytes) -> None: ...
    def read_json(self, rel: str) -> Any: ...
    def write_json(self, rel: str, data: Any) -> None: ...
    def append_jsonl(self, rel: str, records: list[dict]) -> int: ...
    def locked_update_json(
        self, rel: str, default: Any, updater: Callable[[Any], Any]
    ) -> Any: ...
    def exists(self, rel: str) -> bool: ...
    def mkdir(self, rel: str) -> None: ...
    def list(self, rel: str = "", pattern: str = "*") -> list[str]: ...
    def unlink(self, rel: str) -> None: ...
    def path(self, rel: str) -> Path: ...
    @property
    def base_dir(self) -> Path: ...


class LocalMirrorBackend:
    """本地文件系统后端：复用 file_utils 的原子写/锁原语，行为与现状一致。

    镜像根每次调用时经 config.resolve_ex_dir 解析（不缓存），保证对
    config.EXES_DIR 的 monkeypatch 与嵌套/扁平回退语义不受影响。
    """

    name = "local"

    def __init__(self, slug: str, owner: Optional[int] = None):
        self._slug = slug
        self._owner = owner

    # ── 内部定位 ──
    def _root(self) -> Path:
        import config

        return config.resolve_ex_dir(self._slug, self._owner)

    def _path(self, rel: str) -> Path:
        base = self._root()
        rel = (rel or "").strip().replace("\\", "/")
        if rel in ("", "."):
            return base
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        return path_safety.resolve_under(base, *parts)

    def _lock(self, f, mode: int) -> None:
        """阻塞获取文件锁，带超时重试。"""
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"无法获取文件锁: {f.name}")
                time.sleep(0.05)

    # ── 读 ──
    def read_text(self, rel: str, encoding: str = "utf-8") -> str:
        p = self._path(rel)
        return p.read_text(encoding=encoding)

    def read_bytes(self, rel: str) -> bytes:
        return self._path(rel).read_bytes()

    def read_json(self, rel: str) -> Any:
        return json.loads(self.read_text(rel))

    # ── 写 ──
    def write_text(self, rel: str, content: str, encoding: str = "utf-8") -> None:
        p = self._path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        file_utils.atomic_write(p, content, encoding=encoding)

    def write_bytes(self, rel: str, data: bytes) -> None:
        p = self._path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def write_json(self, rel: str, data: Any) -> None:
        self.write_text(rel, json.dumps(data, ensure_ascii=False, indent=2))

    def append_jsonl(self, rel: str, records: list[dict]) -> int:
        """追加 JSONL 记录（带跨进程文件锁，语义同 conversation store）。"""
        if not records:
            return 0
        p = self._path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        lock_path = p.with_name(p.name + ".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            self._lock(lock_file, fcntl.LOCK_EX)
            with open(p, "a", encoding="utf-8") as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
        return len(records)

    def locked_update_json(
        self, rel: str, default: Any, updater: Callable[[Any], Any]
    ) -> Any:
        """在同一把文件锁内完成 JSON 读-改-写，返回 updater 的结果。"""
        p = self._path(rel)
        return file_utils.locked_update_json(p, default, updater)

    # ── 目录/存在 ──
    def exists(self, rel: str) -> bool:
        return self._path(rel).exists()

    def mkdir(self, rel: str) -> None:
        self._path(rel).mkdir(parents=True, exist_ok=True)

    def list(self, rel: str = "", pattern: str = "*") -> list[str]:
        """列出镜像内某目录下的文件（相对路径），目录不存在返回空列表。"""
        base_dir = self._path(rel)
        if not base_dir.exists():
            return []
        root = self._root()
        results = []
        for p in sorted(base_dir.glob(pattern)):
            if p.is_file():
                results.append(p.relative_to(root).as_posix())
        return results

    def unlink(self, rel: str) -> None:
        p = self._path(rel)
        p.unlink(missing_ok=True)

    # ── 高级操作出口 ──
    def path(self, rel: str) -> Path:
        return self._path(rel)

    @property
    def base_dir(self) -> Path:
        return self._root()


class MirrorStore:
    """面向调用方的薄门面，转发到具体后端，并暴露 path()/base_dir 出口。"""

    def __init__(self, backend: MirrorBackend):
        self._backend = backend
        self.name = backend.name

    def read_text(self, rel: str, encoding: str = "utf-8") -> str:
        return self._backend.read_text(rel, encoding=encoding)

    def read_bytes(self, rel: str) -> bytes:
        return self._backend.read_bytes(rel)

    def write_text(self, rel: str, content: str, encoding: str = "utf-8") -> None:
        self._backend.write_text(rel, content, encoding=encoding)

    def write_bytes(self, rel: str, data: bytes) -> None:
        self._backend.write_bytes(rel, data)

    def read_json(self, rel: str) -> Any:
        return self._backend.read_json(rel)

    def write_json(self, rel: str, data: Any) -> None:
        self._backend.write_json(rel, data)

    def append_jsonl(self, rel: str, records: list[dict]) -> int:
        return self._backend.append_jsonl(rel, records)

    def locked_update_json(
        self, rel: str, default: Any, updater: Callable[[Any], Any]
    ) -> Any:
        return self._backend.locked_update_json(rel, default, updater)

    def exists(self, rel: str) -> bool:
        return self._backend.exists(rel)

    def mkdir(self, rel: str) -> None:
        self._backend.mkdir(rel)

    def list(self, rel: str = "", pattern: str = "*") -> list[str]:
        return self._backend.list(rel, pattern)

    def unlink(self, rel: str) -> None:
        self._backend.unlink(rel)

    def path(self, rel: str) -> Path:
        """返回镜像内安全 Path，供无法抽象的高级操作（版本备份、chroma、快照）使用。"""
        return self._backend.path(rel)

    @property
    def base_dir(self) -> Path:
        return self._backend.base_dir


def mirror_store(slug: str, owner: Optional[int] = None) -> MirrorStore:
    """创建镜像门面，按 config.MIRROR_STORAGE 选择后端（当前仅 local）。"""
    import config

    backend_type = config.MIRROR_STORAGE
    if backend_type == "local":
        backend: MirrorBackend = LocalMirrorBackend(slug, owner)
    else:  # 预留：将来 s3 等对象存储后端
        raise ValueError(f"不支持的镜像存储后端: {backend_type}")
    return MirrorStore(backend)