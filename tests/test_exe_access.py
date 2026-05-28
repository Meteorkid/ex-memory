"""镜像访问控制测试。"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from core.exe_access import set_owner_user_id, assert_exe_access, user_owns_exe


@pytest.fixture
def exe_dir(tmp_path, monkeypatch):
    slug = "testslug"
    ex = tmp_path / "exes" / slug
    ex.mkdir(parents=True)
    (ex / "meta.json").write_text(
        json.dumps({"name": "T", "slug": slug, "owner_user_id": 1}),
        encoding="utf-8",
    )
    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    monkeypatch.setattr("config.get_ex_dir", lambda s: tmp_path / "exes" / s)
    monkeypatch.setattr("core.exe_access.get_ex_dir", lambda s: tmp_path / "exes" / s)
    return slug


def test_owner_access(exe_dir):
    assert user_owns_exe(exe_dir, 1)
    assert not user_owns_exe(exe_dir, 2)
    assert_exe_access(exe_dir, 1)


def test_denied_without_owner(exe_dir, monkeypatch):
    monkeypatch.setattr("config.SINGLE_USER_MODE", False)
    from config import get_ex_dir
    meta_path = get_ex_dir(exe_dir) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("owner_user_id")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(PermissionError):
        assert_exe_access(exe_dir, 1)


def test_single_user_mode(monkeypatch, exe_dir):
    monkeypatch.setattr("config.SINGLE_USER_MODE", True)
    meta_path = __import__("config").get_ex_dir(exe_dir) / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("owner_user_id")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert_exe_access(exe_dir, 99)
