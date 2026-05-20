"""图片贴纸管理器：扫描内置/自定义贴纸、上传、删除。"""

import json
import re
import uuid
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from core.file_utils import atomic_write_json, locked_update_json
from core.path_safety import resolve_under

logger = logging.getLogger("ex-memory")

STICKERS_DIR = Path(__file__).parent.parent / "web" / "static" / "stickers"
BUILTIN_DIR = STICKERS_DIR / "builtin"
CUSTOM_BASE = Path(__file__).parent.parent / "data" / "stickers" / "custom"

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2MB
MAX_LABEL_LEN = 32
_LABEL_RE = re.compile(r"^[\w\u4e00-\u9fff\- ]{1,32}$")

BUILTIN_CATEGORIES = {
    "happy": "happy",
    "sad": "sad",
    "angry": "angry",
    "cute": "cute",
    "playful": "playful",
    "love": "love",
}


def _custom_dir(user_id: Optional[int] = None) -> Path:
    if user_id is None:
        return CUSTOM_BASE / "_legacy"
    return CUSTOM_BASE / f"u{user_id}"


def _custom_meta_path(user_id: Optional[int] = None) -> Path:
    return _custom_dir(user_id) / "custom.json"


def _sanitize_label(label: str, fallback: str = "") -> str:
    label = (label or fallback or "贴纸").strip()[:MAX_LABEL_LEN]
    if not _LABEL_RE.match(label):
        raise ValueError("贴纸标签仅允许中英文、数字、空格与连字符")
    return label


def _load_custom_meta(user_id: Optional[int] = None) -> list[dict]:
    path = _custom_meta_path(user_id)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取 custom.json 失败: %s", e)
        return []


def _save_custom_meta(items: list[dict], user_id: Optional[int] = None):
    d = _custom_dir(user_id)
    d.mkdir(parents=True, exist_ok=True)
    path = _custom_meta_path(user_id)
    atomic_write_json(path, items)


def _scan_builtin() -> list[dict]:
    result = []
    if not BUILTIN_DIR.exists():
        return result
    for category_dir in BUILTIN_DIR.iterdir():
        if not category_dir.is_dir():
            continue
        category = BUILTIN_CATEGORIES.get(category_dir.name, category_dir.name)
        for f in sorted(category_dir.iterdir()):
            if f.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            sticker_type = "gif" if f.suffix.lower() == ".gif" else "image"
            result.append({
                "id": f"builtin_{category_dir.name}_{f.stem}",
                "type": sticker_type,
                "url": f"/static/stickers/builtin/{category_dir.name}/{f.name}",
                "label": f.stem,
                "category": category,
                "source": "builtin",
            })
    return result


def _scan_custom(user_id: Optional[int] = None) -> list[dict]:
    items = _load_custom_meta(user_id)
    custom_dir = _custom_dir(user_id)
    result = []
    for item in items:
        try:
            filepath = resolve_under(custom_dir, item.get("filename", ""))
        except ValueError:
            continue
        if not filepath.exists():
            continue
        sticker_type = "gif" if filepath.suffix.lower() == ".gif" else "image"
        result.append({
            "id": item["id"],
            "type": sticker_type,
            "url": f"/api/stickers/{item['id']}/content",
            "label": item.get("label", ""),
            "category": item.get("category", "custom"),
            "source": "custom",
            "owner_user_id": item.get("owner_user_id"),
        })
    return result


def list_stickers(category: str = "all", user_id: Optional[int] = None) -> list[dict]:
    """返回 builtin + 当前用户自定义贴纸。"""
    all_stickers = _scan_builtin()
    if user_id is not None:
        all_stickers += _scan_custom(user_id)
    else:
        if CUSTOM_BASE.exists():
            for user_dir in CUSTOM_BASE.iterdir():
                if user_dir.is_dir() and user_dir.name.startswith("u"):
                    try:
                        uid = int(user_dir.name[1:])
                        all_stickers += _scan_custom(uid)
                    except ValueError:
                        pass
            all_stickers += _scan_custom(None)
    if category == "all":
        return all_stickers
    if category == "custom":
        return [s for s in all_stickers if s["source"] == "custom"]
    return [s for s in all_stickers if s["category"] == category]


def get_sticker(sticker_id: str, user_id: Optional[int] = None) -> Optional[dict]:
    for s in list_stickers("all", user_id=user_id):
        if s["id"] == sticker_id:
            return s
    return None


def upload_sticker(
    file_content: bytes,
    filename: str,
    label: str,
    category: str,
    user_id: int,
) -> dict:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {ext}，允许: {', '.join(ALLOWED_EXTENSIONS)}")
    if len(file_content) > MAX_FILE_SIZE:
        raise ValueError(f"文件过大，最大 {MAX_FILE_SIZE // 1024 // 1024}MB")

    label = _sanitize_label(label, Path(filename).stem)
    custom_dir = _custom_dir(user_id)
    custom_dir.mkdir(parents=True, exist_ok=True)

    sticker_id = f"custom_{uuid.uuid4().hex[:12]}"
    save_name = f"{sticker_id}{ext}"
    save_path = custom_dir / save_name
    save_path.write_bytes(file_content)

    meta = {
        "id": sticker_id,
        "filename": save_name,
        "label": label,
        "category": category or "custom",
        "created_at": datetime.now().isoformat(),
        "owner_user_id": user_id,
    }

    def update(items: list[dict]) -> dict:
        items.append(meta)
        return dict(meta)

    try:
        locked_update_json(_custom_meta_path(user_id), list, update)
    except Exception:
        save_path.unlink(missing_ok=True)
        raise

    sticker_type = "gif" if ext == ".gif" else "image"
    return {
        "id": sticker_id,
        "type": sticker_type,
        "url": f"/api/stickers/{sticker_id}/content",
        "label": meta["label"],
        "category": meta["category"],
        "source": "custom",
    }


def delete_sticker(sticker_id: str, user_id: int) -> bool:
    if sticker_id.startswith("builtin_"):
        return False

    def update(items: list[dict]) -> bool:
        target = None
        for item in items:
            if item["id"] == sticker_id:
                target = item
                break
        if not target:
            return False
        if target.get("owner_user_id") not in (None, user_id):
            return False

        try:
            filepath = resolve_under(_custom_dir(user_id), target["filename"])
        except ValueError:
            return False
        if filepath.exists():
            filepath.unlink()

        items[:] = [i for i in items if i["id"] != sticker_id]
        return True

    return bool(locked_update_json(_custom_meta_path(user_id), list, update))


def get_custom_sticker_path(sticker_id: str, user_id: int) -> Optional[Path]:
    """返回当前用户自定义贴纸文件路径。内置贴纸不走该接口。"""
    if sticker_id.startswith("builtin_"):
        return None

    for item in _load_custom_meta(user_id):
        if item.get("id") != sticker_id:
            continue
        if item.get("owner_user_id") not in (None, user_id):
            return None
        try:
            filepath = resolve_under(_custom_dir(user_id), item["filename"])
        except (KeyError, ValueError):
            return None
        if filepath.exists():
            return filepath
    return None
