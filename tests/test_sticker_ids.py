"""贴纸 ID 与扫描一致性。"""

from core.sticker_manager import _scan_builtin
from core.sticker_selector import IMAGE_STICKERS


def test_image_sticker_ids_match_scan():
    scanned = {s["id"] for s in _scan_builtin()}
    for sid in IMAGE_STICKERS:
        assert sid in scanned, f"missing builtin file for {sid}"


def test_hyphen_sticker_ids():
    assert "builtin_cute_heart-eyes" in IMAGE_STICKERS
