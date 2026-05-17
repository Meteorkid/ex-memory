"""图片贴纸管理器：扫描内置/自定义贴纸、上传、删除。"""

import json
import uuid
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger("ex-memory")

# 贴纸根目录（相对于项目根）
STICKERS_DIR = Path(__file__).parent.parent / "web" / "static" / "stickers"
BUILTIN_DIR = STICKERS_DIR / "builtin"
CUSTOM_DIR = STICKERS_DIR / "custom"
CUSTOM_META = CUSTOM_DIR / "custom.json"

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2MB

# 内置贴纸分类映射（目录名 → 情绪类别）
BUILTIN_CATEGORIES = {
    "happy": "happy",
    "sad": "sad",
    "angry": "angry",
    "cute": "cute",
    "playful": "playful",
    "love": "love",
}


def _load_custom_meta() -> list[dict]:
    """读取自定义贴纸元数据。"""
    if not CUSTOM_META.exists():
        return []
    try:
        return json.loads(CUSTOM_META.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("读取 custom.json 失败: %s", e)
        return []


def _save_custom_meta(items: list[dict]):
    """写入自定义贴纸元数据。"""
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_META.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _scan_builtin() -> list[dict]:
    """扫描 builtin/ 目录下的图片/GIF 文件。"""
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


def _scan_custom() -> list[dict]:
    """扫描自定义贴纸（元数据 + 文件校验）。"""
    items = _load_custom_meta()
    result = []
    for item in items:
        filepath = CUSTOM_DIR / item.get("filename", "")
        if not filepath.exists():
            continue
        sticker_type = "gif" if filepath.suffix.lower() == ".gif" else "image"
        result.append({
            "id": item["id"],
            "type": sticker_type,
            "url": f"/static/stickers/custom/{item['filename']}",
            "label": item.get("label", ""),
            "category": item.get("category", "custom"),
            "source": "custom",
        })
    return result


def list_stickers(category: str = "all") -> list[dict]:
    """返回所有图片贴纸（builtin + custom），支持按分类过滤。"""
    all_stickers = _scan_builtin() + _scan_custom()
    if category == "all":
        return all_stickers
    if category == "custom":
        return [s for s in all_stickers if s["source"] == "custom"]
    return [s for s in all_stickers if s["category"] == category]


def get_sticker(sticker_id: str) -> Optional[dict]:
    """按 ID 获取单个贴纸。"""
    for s in list_stickers():
        if s["id"] == sticker_id:
            return s
    return None


def upload_sticker(file_content: bytes, filename: str, label: str, category: str) -> dict:
    """上传自定义贴纸。

    Args:
        file_content: 文件二进制内容
        filename: 原始文件名
        label: 贴纸标签
        category: 情绪分类

    Returns:
        新贴纸信息 dict

    Raises:
        ValueError: 文件类型/大小不合法
    """
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型: {ext}，允许: {', '.join(ALLOWED_EXTENSIONS)}")
    if len(file_content) > MAX_FILE_SIZE:
        raise ValueError(f"文件过大，最大 {MAX_FILE_SIZE // 1024 // 1024}MB")

    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)

    sticker_id = f"custom_{uuid.uuid4().hex[:12]}"
    save_name = f"{sticker_id}{ext}"
    save_path = CUSTOM_DIR / save_name

    save_path.write_bytes(file_content)

    meta = {
        "id": sticker_id,
        "filename": save_name,
        "label": label or Path(filename).stem,
        "category": category or "custom",
        "created_at": datetime.now().isoformat(),
    }

    items = _load_custom_meta()
    items.append(meta)
    _save_custom_meta(items)

    sticker_type = "gif" if ext == ".gif" else "image"
    return {
        "id": sticker_id,
        "type": sticker_type,
        "url": f"/static/stickers/custom/{save_name}",
        "label": meta["label"],
        "category": meta["category"],
        "source": "custom",
    }


def delete_sticker(sticker_id: str) -> bool:
    """删除自定义贴纸。builtin 贴纸不可删除。

    Returns:
        True 删除成功，False 未找到或为 builtin
    """
    if sticker_id.startswith("builtin_"):
        return False

    items = _load_custom_meta()
    target = None
    for item in items:
        if item["id"] == sticker_id:
            target = item
            break

    if not target:
        return False

    # 删除文件
    filepath = CUSTOM_DIR / target["filename"]
    if filepath.exists():
        filepath.unlink()

    # 更新元数据
    items = [i for i in items if i["id"] != sticker_id]
    _save_custom_meta(items)
    return True
