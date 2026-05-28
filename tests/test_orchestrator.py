"""pipeline/orchestrator.py 测试：API 创建流程。"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def clean_engine_cache():
    """每次测试前清空 engine 缓存。"""
    from server.routes import _engine_cache
    _engine_cache.clear()
    yield
    _engine_cache.clear()


def test_create_flow_api_success():
    """正常创建流程：从头到尾完成所有步骤。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        with patch("pipeline.orchestrator.get_ex_dir", return_value=tmpdir), \
             patch("pipeline.orchestrator.ensure_ex_dirs", return_value=tmpdir), \
             patch("pipeline.orchestrator.get_embedding_config", return_value={
                 "api_key": "test", "base_url": "http://test", "model": "test"
             }), \
             patch("pipeline.orchestrator.get_llm_config", return_value={
                 "api_key": "test-key", "model": "test"
             }), \
             patch("pipeline.orchestrator.get_collection_name", return_value="test_collection"), \
             patch("pipeline.orchestrator.build_memory", return_value="# Memory\n测试记忆"), \
             patch("pipeline.orchestrator.build_persona", return_value="# Persona\n测试人格"), \
             patch("pipeline.orchestrator.write_skill") as mock_write_skill, \
             patch("pipeline.orchestrator.version_backup"), \
             patch("pipeline.orchestrator.Embedder"), \
             patch("pipeline.orchestrator.VectorStore"), \
             patch("pipeline.orchestrator.Chunker"):

            from pipeline.orchestrator import run_create_flow_api
            result = run_create_flow_api(
                slug="test", name="测试", answers=["基本信息", "性格"]
            )

            assert result["state"] == "completed"
            assert result["slug"] == "test"
            mock_write_skill.assert_called_once_with("test")

            # 验证 meta.json 已更新
            meta = json.loads((tmpdir / "meta.json").read_text(encoding="utf-8"))
            assert meta["pipeline_state"] == "completed"


def test_create_flow_api_resume():
    """恢复模式：从失败步骤继续。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        # 创建失败状态的 meta.json
        meta = {
            "name": "测试", "slug": "test",
            "pipeline_state": "failed",
            "failed_step": "distill_persona",
            "profile": {"basic_info": "info", "personality": "personality"},
        }
        (tmpdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))

        with patch("pipeline.orchestrator.get_ex_dir", return_value=tmpdir), \
             patch("pipeline.orchestrator.get_embedding_config", return_value={
                 "api_key": "test", "base_url": "http://test", "model": "test"
             }), \
             patch("pipeline.orchestrator.get_llm_config", return_value={
                 "api_key": "test-key", "model": "test"
             }), \
             patch("pipeline.orchestrator.get_collection_name", return_value="test_collection"), \
             patch("pipeline.orchestrator.build_persona", return_value="# Persona\n测试人格"), \
             patch("pipeline.orchestrator.write_skill"), \
             patch("pipeline.orchestrator.version_backup"), \
             patch("pipeline.orchestrator.Embedder"), \
             patch("pipeline.orchestrator.VectorStore"), \
             patch("pipeline.orchestrator.Chunker"):

            from pipeline.orchestrator import run_create_flow_api
            result = run_create_flow_api(
                slug="test", name="测试", answers=[], resume=True
            )

            assert result["state"] == "completed"
            assert result["slug"] == "test"


def test_create_flow_api_no_llm_key():
    """未配置 LLM API Key 时返回错误。"""
    with patch("pipeline.orchestrator.get_llm_config", return_value={"api_key": ""}):
        from pipeline.orchestrator import run_create_flow_api
        result = run_create_flow_api(slug="test", name="测试", answers=[])
        assert "error" in result
        assert "API Key" in result["error"]


def test_create_flow_api_no_resume_no_meta():
    """恢复模式但找不到 meta.json。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        with patch("pipeline.orchestrator.get_ex_dir", return_value=tmpdir):
            from pipeline.orchestrator import run_create_flow_api
            result = run_create_flow_api(
                slug="test", name="测试", answers=[], resume=True
            )
            assert "error" in result


def test_create_flow_api_resume_not_failed():
    """恢复模式但镜像未处于失败状态。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        meta = {
            "name": "测试", "slug": "test",
            "pipeline_state": "completed",
        }
        (tmpdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        with patch("pipeline.orchestrator.get_ex_dir", return_value=tmpdir):
            from pipeline.orchestrator import run_create_flow_api
            result = run_create_flow_api(
                slug="test", name="测试", answers=[], resume=True
            )
            assert "error" in result


def test_create_flow_api_memory_failure():
    """蒸馏 memory 步骤失败时返回错误。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        with patch("pipeline.orchestrator.get_ex_dir", return_value=tmpdir), \
             patch("pipeline.orchestrator.ensure_ex_dirs", return_value=tmpdir), \
             patch("pipeline.orchestrator.get_embedding_config", return_value={
                 "api_key": "test", "base_url": "http://test", "model": "test"
             }), \
             patch("pipeline.orchestrator.get_llm_config", return_value={
                 "api_key": "test-key", "model": "test"
             }), \
             patch("pipeline.orchestrator.get_collection_name", return_value="test_collection"), \
             patch("pipeline.orchestrator.build_memory", side_effect=RuntimeError("LLM 调用失败")), \
             patch("pipeline.orchestrator.version_backup"), \
             patch("pipeline.orchestrator.Embedder"), \
             patch("pipeline.orchestrator.VectorStore"), \
             patch("pipeline.orchestrator.Chunker"):

            from pipeline.orchestrator import run_create_flow_api
            result = run_create_flow_api(
                slug="test", name="测试", answers=["info"]
            )
            assert "error" in result
            assert "memory" in result["error"].lower()
