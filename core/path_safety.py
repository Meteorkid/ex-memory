"""路径安全工具。"""

import re
from pathlib import Path

_VERSION_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-\u4e00-\u9fff]{1,64}$")


def safe_filename(name: str) -> str:
    """剥离路径成分，仅保留 basename。"""
    if not name:
        raise ValueError("文件名不能为空")
    base = Path(name).name
    if not base or base in (".", ".."):
        raise ValueError("无效的文件名")
    return base


def safe_version_name(name: str) -> str:
    """校验版本目录名，禁止路径分隔符。"""
    name = name.strip()
    if not name:
        raise ValueError("版本名不能为空")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("版本名包含非法字符")
    if not _VERSION_NAME_RE.match(name):
        raise ValueError("版本名格式无效")
    return name


def resolve_under(base: Path, *parts: str) -> Path:
    """解析路径并确保落在 base 目录内。"""
    target = (base.joinpath(*parts)).resolve()
    base_resolved = base.resolve()
    if not target.is_relative_to(base_resolved):
        raise ValueError("路径越界")
    return target
