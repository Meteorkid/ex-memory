"""镜像记忆版本：独立 Web/MCP 进程通过共享文件观察同一次更新。"""

from datetime import datetime, timezone
import json
import uuid
from typing import Optional

STATE_FILE = ".memory_revision.json"
_MEMORY_FILES = {
    "SKILL.md",
    "persona.md",
    "memory.md",
    "corrections.json",
    "corrections.md",
    "meta.json",
    "corpus.jsonl",
    "memory_index.json",
}


def is_memory_file(rel: str) -> bool:
    name = rel.replace("\\", "/").lstrip("./")
    return name in _MEMORY_FILES or (
        name.startswith("sessions/")
        and (name.endswith(".md") or name.endswith(".summary.json"))
    )


def get_memory_revision(slug: str, owner: Optional[int] = None) -> int:
    from core.mirror_store import mirror_store

    store = mirror_store(slug, owner)
    if not store.exists(STATE_FILE):
        return 0
    try:
        state = store.read_json(STATE_FILE)
        revision = state["revision"]
        if type(revision) is not int or revision < 0:
            raise ValueError("invalid revision")
        return revision
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("记忆版本不可读，请检查镜像状态") from exc


def get_memory_signature(slug: str, owner: Optional[int] = None) -> tuple[str, int]:
    """缓存身份包含镜像代次，删除重建不能复用旧 revision 对应的引擎。"""
    from core.mirror_store import mirror_store

    store = mirror_store(slug, owner)
    state = store.read_json(STATE_FILE) if store.exists(STATE_FILE) else {"revision": 0}
    if not isinstance(state, dict):
        raise ValueError("记忆版本不可读，请检查镜像状态")
    revision = state.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("记忆版本无效")
    if "generation" in state:
        generation = state["generation"]
        if not _valid_generation(generation):
            raise ValueError("记忆代次无效")
        return generation, revision
    try:
        metadata = store.base_dir.stat()
    except FileNotFoundError:
        return "missing", revision
    birth_ns = getattr(metadata, "st_birthtime_ns", None)
    if birth_ns is None:
        birth = getattr(metadata, "st_birthtime", None)
        birth_ns = (
            int(birth * 1_000_000_000) if birth is not None else metadata.st_ctime_ns
        )
    # 旧镜像读取不能偷偷创建状态；目录 inode 与创建时间区分其生命周期。
    generation = f"legacy:{metadata.st_dev}:{metadata.st_ino}:{birth_ns}"
    return generation, revision


def _valid_generation(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


def advance_memory_state(state: dict) -> int:
    revision = state.get("revision", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("记忆版本无效")
    if "generation" not in state:
        state["generation"] = uuid.uuid4().hex
    elif not _valid_generation(state["generation"]):
        raise ValueError("记忆代次无效")
    state["revision"] = revision + 1
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state["revision"]


def touch_memory(slug: str, owner: Optional[int] = None) -> int:
    from core.mirror_store import mirror_store

    return mirror_store(slug, owner).locked_update_json(
        STATE_FILE, dict, advance_memory_state
    )
