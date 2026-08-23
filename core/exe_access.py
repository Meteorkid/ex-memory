"""镜像访问控制：owner 绑定与单人模式。"""

import json
import logging
from typing import Optional

from config import get_ex_dir
from core.file_utils import atomic_write_json

logger = logging.getLogger("ex-memory")


def load_meta(slug: str) -> Optional[dict]:
    meta_path = get_ex_dir(slug) / "meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_owner_user_id(slug: str) -> Optional[int]:
    meta = load_meta(slug)
    if not meta:
        return None
    owner = meta.get("owner_user_id")
    return int(owner) if owner is not None else None


def set_owner_user_id(slug: str, user_id: int) -> None:
    ex_dir = get_ex_dir(slug)
    meta_path = ex_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"镜像 [{slug}] 不存在")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["owner_user_id"] = user_id
    atomic_write_json(meta_path, meta)


def _single_user_mode() -> bool:
    from config import SINGLE_USER_MODE
    return SINGLE_USER_MODE


def user_owns_exe(slug: str, user_id: int) -> bool:
    if _single_user_mode():
        return True
    owner = get_owner_user_id(slug)
    if owner is None:
        return False
    return owner == user_id


def assert_exe_access(slug: str, user_id: int) -> None:
    """校验当前用户可访问该镜像。Raises PermissionError。"""
    ex_dir = get_ex_dir(slug)
    if not ex_dir.exists():
        raise FileNotFoundError(f"镜像 [{slug}] 不存在")

    if _single_user_mode():
        owner = get_owner_user_id(slug)
        if owner is None:
            try:
                set_owner_user_id(slug, user_id)
            except Exception as e:
                logger.warning("无法绑定镜像 owner: %s", e)
        return

    owner = get_owner_user_id(slug)
    if owner is None:
        raise PermissionError("该镜像未绑定用户，无法访问")
    if owner != user_id:
        raise PermissionError("无权访问该镜像")


def iter_accessible_exes(user_id: int):
    """迭代当前用户可访问的镜像目录。"""
    from config import EXES_DIR
    if not EXES_DIR.exists():
        return
    for d in EXES_DIR.iterdir():
        if not d.is_dir() or not (d / "meta.json").exists():
            continue
        if _single_user_mode() or user_owns_exe(d.name, user_id):
            yield d
