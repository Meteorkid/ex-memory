"""扫描本机 iTunes/iOS 备份。"""

import plistlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import config
from core.path_safety import resolve_under

_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True)
class WechatBackup:
    id: str
    name: str
    path: str
    updated_at: str
    device_name: str = ""
    product_type: str = ""
    ios_version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def list_backups() -> list[WechatBackup]:
    """列出固定根目录下的 iTunes/iOS 备份。"""
    root = backup_root()
    if not root.exists() or not root.is_dir():
        return []

    backups = []
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink() or not _is_valid_backup_id(path.name):
            continue
        info = _read_info(path)
        updated_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        device_name = str(info.get("Device Name", "") or "")
        product_type = str(info.get("Product Type", "") or "")
        ios_version = str(info.get("Product Version", "") or "")
        name = device_name or path.name
        backups.append(WechatBackup(
            id=path.name,
            name=name,
            path=str(path),
            updated_at=updated_at,
            device_name=device_name,
            product_type=product_type,
            ios_version=ios_version,
        ))
    return sorted(backups, key=lambda item: item.updated_at, reverse=True)


def get_backup_path(backup_id: str) -> Path:
    """按备份 ID 解析目录，并确保不越过固定根目录。"""
    if not _is_valid_backup_id(backup_id):
        raise ValueError("备份 ID 格式无效")
    path = resolve_under(backup_root(), backup_id)
    if not path.exists() or not path.is_dir() or path.is_symlink():
        raise FileNotFoundError("备份不存在")
    return path


def backup_root() -> Path:
    return config.WECHAT_EXPORT_BACKUP_ROOT.expanduser()


def _is_valid_backup_id(backup_id: str) -> bool:
    return bool(_BACKUP_ID_RE.fullmatch((backup_id or "").strip()))


def _read_info(path: Path) -> dict:
    info_path = path / "Info.plist"
    if not info_path.exists() or not info_path.is_file():
        return {}
    try:
        with open(info_path, "rb") as f:
            data = plistlib.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
