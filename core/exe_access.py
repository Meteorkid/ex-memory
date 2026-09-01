"""镜像访问控制：owner 绑定与单人模式。

镜像目录布局：
- 存量（迁移前）：exes/<slug>
- 新建（按账号隔离）：exes/<owner>/<slug>

所有关心租户边界的读写在多用户模式下都应携带 owner，
通过 config.resolve_ex_dir 优先命中嵌套目录、回退扁平目录兼容存量镜像。
"""

import json
import logging
from typing import Optional

from config import resolve_ex_dir
from core.file_utils import atomic_write_json

logger = logging.getLogger("ex-memory")


def load_meta(slug: str, owner: Optional[int] = None) -> Optional[dict]:
    """读取镜像 meta.json。owner 为空时只查扁平目录（存量 / 无人格上下文）。"""
    meta_path = resolve_ex_dir(slug, owner) / "meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_owner_user_id(slug: str, owner: Optional[int] = None) -> Optional[int]:
    """读取镜像绑定的 owner。优先解析到 owner 指定的命名空间目录。"""
    meta = load_meta(slug, owner)
    if not meta:
        return None
    bound = meta.get("owner_user_id")
    return int(bound) if bound is not None else None


def set_owner_user_id(slug: str, user_id: int) -> None:
    ex_dir = resolve_ex_dir(slug, user_id)
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
    owner = get_owner_user_id(slug, user_id)
    if owner is None:
        return False
    return owner == user_id


def assert_exe_access(slug: str, user_id: int) -> None:
    """校验当前用户可访问该镜像。Raises PermissionError。"""
    ex_dir = resolve_ex_dir(slug, user_id)
    if not ex_dir.exists():
        raise FileNotFoundError(f"镜像 [{slug}] 不存在")

    if _single_user_mode():
        owner = get_owner_user_id(slug, user_id)
        if owner is None:
            try:
                set_owner_user_id(slug, user_id)
            except Exception as e:
                logger.warning("无法绑定镜像 owner: %s", e)
        return

    owner = get_owner_user_id(slug, user_id)
    if owner is None:
        raise PermissionError("该镜像未绑定用户，无法访问")
    if owner != user_id:
        raise PermissionError("无权访问该镜像")


def iter_accessible_exes(user_id: int):
    """迭代当前用户可访问的镜像目录（含嵌套与扁平两种布局）。"""
    from config import EXES_DIR

    if not EXES_DIR.exists():
        return

    for top in sorted(EXES_DIR.iterdir()):
        if not top.is_dir():
            continue

        # 扁平镜像：exes/<slug>（含 meta.json）
        if (top / "meta.json").exists():
            if _single_user_mode() or user_owns_exe(top.name, user_id):
                yield top
            continue

        # 嵌套 owner 目录：exes/<owner>/<slug>
        if _single_user_mode() or top.name == str(user_id):
            for sub in sorted(top.iterdir()):
                if sub.is_dir() and (sub / "meta.json").exists():
                    yield sub