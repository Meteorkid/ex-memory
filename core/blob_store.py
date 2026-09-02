"""对象存储抽象：本地目录与 S3 兼容后端。

**当前用途是备份与恢复，不是运行时文件访问。**这是一个有意识的取舍：

镜像目录下的文件（SKILL.md / persona.md / conversations / corpus.jsonl ...）
在 29 个模块里有 74 处直接访问。把它们全部改成对象存储 API 是多日重构，
而这些文件就是产品本身——大范围机械替换引入细微 bug 的风险，高于它带来的
收益。所以：

- 运行时仍走文件系统。**多副本部署要求 exes/ 挂在共享卷上**（NFS/EFS/CSI），
  这一点写进了部署文档。
- 对象存储负责**持久化与灾备**：定期把镜像快照推上去，容器重建或磁盘丢失
  时能恢复。这解决了 NFR-033「平台级备份」从 0 到 1 的问题。
- 逐文件走对象存储是后续独立改动，前提是先有一层 MirrorStore 门面收敛那
  74 处访问。
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Iterator, Optional, Protocol

logger = logging.getLogger("ex-memory")


class BlobBackend(Protocol):
    name: str

    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> Optional[bytes]: ...
    def delete(self, key: str) -> None: ...
    def list_keys(self, prefix: str) -> Iterator[str]: ...
    def exists(self, key: str) -> bool: ...


class LocalBackend:
    """本地目录后端。开发与单机部署用，也是 S3 不可用时的降级目标。"""

    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # 防目录穿越：key 由内部生成，但备份恢复会读到外部数据
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"非法对象键: {key}")
        return target

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, key: str) -> Optional[bytes]:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def list_keys(self, prefix: str) -> Iterator[str]:
        base = self.root
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix == ".tmp":
                continue
            key = path.relative_to(base).as_posix()
            if key.startswith(prefix):
                yield key

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3Backend:
    """S3 兼容后端（AWS S3 / MinIO / 阿里云 OSS 的 S3 接口）。"""

    name = "s3"

    def __init__(self, bucket: str, endpoint: str = "", prefix: str = ""):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client("s3", endpoint_url=endpoint or None)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> Optional[bytes]:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
            return resp["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def list_keys(self, prefix: str) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        full_prefix = self._key(prefix)
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                if self.prefix:
                    key = key[len(self.prefix) + 1 :]
                yield key

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False


_backend: Optional[BlobBackend] = None


def configure(
    bucket: str = "",
    endpoint: str = "",
    prefix: str = "",
    local_root: Optional[Path] = None,
) -> BlobBackend:
    global _backend
    if bucket:
        try:
            _backend = S3Backend(bucket, endpoint, prefix)
            logger.info("对象存储使用 S3: bucket=%s", bucket)
            return _backend
        except Exception as e:  # noqa: BLE001 — 配错了要降级并告警，不能起不来
            logger.error("S3 初始化失败（%s），降级为本地目录", e)
    import config

    root = local_root or (config.PROJECT_DIR / "data" / "blobs")
    _backend = LocalBackend(root)
    return _backend


def backend() -> BlobBackend:
    if _backend is None:
        import config

        configure(
            bucket=config.BLOB_BUCKET,
            endpoint=config.BLOB_ENDPOINT,
            prefix=config.BLOB_PREFIX,
        )
    return _backend  # type: ignore[return-value]


def reset_for_tests() -> None:
    global _backend
    _backend = None


# ── 镜像快照 ──

_SKIP_NAMES = {".DS_Store"}
_SKIP_SUFFIXES = {".lock", ".tmp"}


def _should_backup(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    if path.name in _SKIP_NAMES or path.suffix in _SKIP_SUFFIXES:
        return False
    return True


def snapshot_mirror(slug: str, owner: Optional[int], label: str) -> int:
    """把一个镜像目录整体推到对象存储，返回文件数。

    快照键形如 snapshots/<label>/<owner>/<slug>/<相对路径>，
    恢复时按这个前缀取回。
    """
    import config

    ex_dir = config.resolve_ex_dir(slug, owner)
    if not ex_dir.exists():
        raise FileNotFoundError(f"镜像 [{slug}] 不存在")

    store = backend()
    owner_part = str(owner) if owner is not None else "_flat"
    prefix = f"snapshots/{label}/{owner_part}/{slug}"
    count = 0
    for path in sorted(ex_dir.rglob("*")):
        if not _should_backup(path):
            continue
        key = f"{prefix}/{path.relative_to(ex_dir).as_posix()}"
        store.put(key, path.read_bytes())
        count += 1
    logger.info("镜像快照完成 slug=%s label=%s 文件数=%d", slug, label, count)
    return count


def restore_mirror(slug: str, owner: Optional[int], label: str) -> int:
    """从快照恢复一个镜像，返回恢复的文件数。

    恢复到临时目录后再整体替换：中途失败不会留下半个镜像。
    """
    import config

    store = backend()
    owner_part = str(owner) if owner is not None else "_flat"
    prefix = f"snapshots/{label}/{owner_part}/{slug}"
    keys = list(store.list_keys(prefix))
    if not keys:
        raise FileNotFoundError(f"找不到快照: {prefix}")

    ex_dir = config.resolve_ex_dir(slug, owner)
    staging = ex_dir.with_name(ex_dir.name + ".restoring")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    count = 0
    for key in keys:
        data = store.get(key)
        if data is None:
            continue
        relative = key[len(prefix) + 1 :]
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        count += 1

    if ex_dir.exists():
        shutil.rmtree(ex_dir)
    staging.rename(ex_dir)
    logger.info("镜像恢复完成 slug=%s label=%s 文件数=%d", slug, label, count)
    return count


def list_snapshots() -> list[str]:
    """列出所有快照标签。"""
    labels = set()
    for key in backend().list_keys("snapshots/"):
        parts = key.split("/")
        if len(parts) > 1:
            labels.add(parts[1])
    return sorted(labels)
