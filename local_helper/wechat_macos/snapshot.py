"""微信数据库的只读、可校验快照。"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path


class SnapshotChangedError(RuntimeError):
    """快照期间源文件发生变化。"""


@dataclass(frozen=True)
class SnapshotFile:
    source: Path
    snapshot: Path
    sha256: str
    size: int


def create_database_snapshot(source_db: Path, destination_dir: Path) -> tuple[SnapshotFile, ...]:
    if source_db.is_symlink():
        raise ValueError("数据库必须是普通文件")
    source_db = source_db.resolve(strict=True)
    if not source_db.is_file():
        raise ValueError("数据库必须是普通文件")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_dir = destination_dir.resolve()

    sources = [source_db]
    for suffix in ("-wal", "-shm"):
        sibling = source_db.with_name(source_db.name + suffix)
        if sibling.is_file() and not sibling.is_symlink():
            sources.append(sibling)

    required = sum(path.stat().st_size for path in sources)
    if shutil.disk_usage(destination_dir).free < required * 2 + 16 * 1024 * 1024:
        raise OSError("剩余磁盘空间不足以创建安全快照")

    results: list[SnapshotFile] = []
    for source in sources:
        before = _sha256(source)
        target = destination_dir / source.name
        if target.exists():
            raise FileExistsError(target)
        shutil.copyfile(source, target)
        after = _sha256(source)
        copied = _sha256(target)
        if before != after or before != copied:
            target.unlink(missing_ok=True)
            raise SnapshotChangedError(f"快照期间数据库发生变化: {source.name}")
        results.append(SnapshotFile(source=source, snapshot=target, sha256=copied, size=target.stat().st_size))
    return tuple(results)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
