"""版本管理 — 测试。"""

import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch
from core.version_manager import backup, rollback, list_versions


@pytest.fixture
def temp_ex_dir():
    """创建临时镜像目录，模拟已有文件。"""
    tmp = tempfile.mkdtemp()
    ex_dir = Path(tmp) / "exes" / "test_user"
    ex_dir.mkdir(parents=True)
    # 创建几个关键文件
    (ex_dir / "SKILL.md").write_text("# SKILL\n版本一", encoding="utf-8")
    (ex_dir / "memory.md").write_text("# Memory\n版本一", encoding="utf-8")
    (ex_dir / "persona.md").write_text("# Persona\n版本一", encoding="utf-8")
    (ex_dir / "meta.json").write_text('{"name":"test"}', encoding="utf-8")
    yield ex_dir
    shutil.rmtree(tmp, ignore_errors=True)


class TestVersionManager:
    def test_backup_creates_version(self, temp_ex_dir):
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            version_name = backup("test_user")
            version_path = temp_ex_dir / "versions" / version_name
            assert version_path.exists()
            assert (version_path / "SKILL.md").read_text() == "# SKILL\n版本一"
            assert (version_path / "memory.md").read_text() == "# Memory\n版本一"

    def test_backup_creates_meta(self, temp_ex_dir):
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            version_name = backup("test_user", "v1_custom")
            assert version_name == "v1_custom"
            meta_path = temp_ex_dir / "versions" / "v1_custom" / "version_meta.json"
            assert meta_path.exists()

    def test_rollback_restores_files(self, temp_ex_dir):
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            # 先备份
            version_name = backup("test_user")
            # 修改原文件
            (temp_ex_dir / "memory.md").write_text("# Memory\n修改后", encoding="utf-8")
            # 回滚
            rollback("test_user", version_name)
            assert (temp_ex_dir / "memory.md").read_text() == "# Memory\n版本一"

    def test_rollback_nonexistent_version(self, temp_ex_dir):
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            with pytest.raises(FileNotFoundError, match="版本不存在"):
                rollback("test_user", "v_nonexistent")

    def test_list_versions(self, temp_ex_dir):
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            backup("test_user", "v1")
            backup("test_user", "v2")
            versions = list_versions("test_user")
            assert len(versions) == 2

    def test_list_versions_empty(self, temp_ex_dir):
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            versions = list_versions("test_user")
            assert versions == []

    def test_backup_nonexistent_ex(self, temp_ex_dir):
        with patch("core.version_manager.get_ex_dir", return_value=Path("/nonexistent")):
            with pytest.raises(FileNotFoundError, match="镜像不存在"):
                backup("ghost")

    def test_backup_includes_chromadb(self, temp_ex_dir):
        """备份应包含 chroma_db 目录。"""
        chroma_dir = temp_ex_dir / "chroma_db"
        chroma_dir.mkdir()
        (chroma_dir / "test.db").write_text("fake-db-content")
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            version_name = backup("test_user")
            version_chroma = temp_ex_dir / "versions" / version_name / "chroma_db"
            assert version_chroma.exists()
            assert (version_chroma / "test.db").read_text() == "fake-db-content"

    def test_backup_skips_chromadb_when_disabled(self, temp_ex_dir):
        """include_chroma=False 时不备份向量库。"""
        chroma_dir = temp_ex_dir / "chroma_db"
        chroma_dir.mkdir()
        (chroma_dir / "test.db").write_text("fake")
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            version_name = backup("test_user", include_chroma=False)
            version_chroma = temp_ex_dir / "versions" / version_name / "chroma_db"
            assert not version_chroma.exists()

    def test_rollback_restores_chromadb(self, temp_ex_dir):
        """回滚应恢复 chroma_db。"""
        chroma_dir = temp_ex_dir / "chroma_db"
        chroma_dir.mkdir()
        (chroma_dir / "test.db").write_text("v1-db")
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            # 备份 v1
            version_name = backup("test_user")
            # 修改向量库
            (chroma_dir / "test.db").write_text("v2-modified")
            # 回滚
            rollback("test_user", version_name)
            assert (chroma_dir / "test.db").read_text() == "v1-db"

    def test_backup_skips_empty_chromadb(self, temp_ex_dir):
        """空 chroma_db 不备份（没有内容的目录不拷贝）。"""
        chroma_dir = temp_ex_dir / "chroma_db"
        chroma_dir.mkdir()  # 空目录
        with patch("core.version_manager.get_ex_dir", return_value=temp_ex_dir):
            version_name = backup("test_user")
            version_chroma = temp_ex_dir / "versions" / version_name / "chroma_db"
            assert not version_chroma.exists()
