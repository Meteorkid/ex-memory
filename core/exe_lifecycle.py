"""镜像数据生命周期：导出与彻底删除。"""

import shutil
import tempfile
import zipfile
import json
from datetime import datetime, timezone
from pathlib import Path

import config

_SKIP_NAMES = {".DS_Store"}
_SKIP_SUFFIXES = {".lock", ".tmp"}


def create_exe_export(slug: str) -> Path:
    """将镜像目录打包为临时 zip 文件，返回 zip 路径。"""
    ex_dir = config.get_ex_dir(slug)
    if not ex_dir.exists():
        raise FileNotFoundError(f"镜像 [{slug}] 不存在")

    tmp = tempfile.NamedTemporaryFile(prefix=f"ex-memory-{slug}-", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                f"{slug}/export_manifest.json",
                _export_manifest(slug),
            )
            for path in sorted(ex_dir.rglob("*")):
                if not _should_export(path):
                    continue
                arcname = Path(slug) / path.relative_to(ex_dir)
                zf.write(path, arcname.as_posix())
        return tmp_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def delete_exe_data(slug: str) -> None:
    """彻底删除镜像目录，包括 sessions、versions、wallet 与向量库。"""
    ex_dir = config.get_ex_dir(slug)
    if not ex_dir.exists():
        raise FileNotFoundError(f"镜像 [{slug}] 不存在")
    shutil.rmtree(ex_dir)


def _should_export(path: Path) -> bool:
    if path.is_dir() or path.is_symlink():
        return False
    if path.name in _SKIP_NAMES:
        return False
    return path.suffix not in _SKIP_SUFFIXES


def _export_manifest(slug: str) -> str:
    return json.dumps({
        "slug": slug,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "format": "ex-memory-exe-export-v1",
    }, ensure_ascii=False, indent=2)
