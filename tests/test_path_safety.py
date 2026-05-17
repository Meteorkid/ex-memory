"""路径安全工具测试。"""

import pytest
from pathlib import Path
from core.path_safety import safe_filename, safe_version_name, resolve_under


def test_safe_filename_strips_path():
    assert safe_filename("../../etc/passwd") == "passwd"


def test_safe_version_name_rejects_traversal():
    with pytest.raises(ValueError):
        safe_version_name("../evil")


def test_resolve_under_blocks_escape(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(ValueError):
        resolve_under(base, "..", "outside")
