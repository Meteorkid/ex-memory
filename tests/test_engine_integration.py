"""core/engine.py ChatEngine 集成测试。"""

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _create_test_engine(tmpdir):
    """创建一个用于测试的 ChatEngine。"""
    tmpdir = Path(tmpdir)
    (tmpdir / "SKILL.md").write_text("# 测试人格\n你是一个测试助手。")
    sessions_dir = tmpdir / "sessions"
    sessions_dir.mkdir()

    with patch("core.engine.get_ex_dir", return_value=tmpdir), \
         patch("core.engine.get_llm_config", return_value={
             "model": "test", "temperature": 0.8, "top_p": 0.9,
             "frequency_penalty": 0.6, "max_tokens": 4096
         }), \
         patch("core.engine.get_llm_client") as mock_client:
        mock_client.return_value = MagicMock()
        from core.engine import ChatEngine
        engine = ChatEngine("test", vector_store=None, embedder=None)
        return engine, mock_client.return_value


def test_chat_returns_reply():
    """chat 方法返回正常回复。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, mock_client = _create_test_engine(tmpdir)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="你好！"))]
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
        mock_client.chat.completions.create.return_value = mock_response

        reply, stickers, usage = engine.chat("hello", [])
        assert reply == "你好！"
        assert isinstance(stickers, list)
        assert usage.prompt_tokens == 10


def test_chat_stream_yields_text():
    """chat_stream 方法 yield 文本。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, mock_client = _create_test_engine(tmpdir)

        chunk1 = MagicMock()
        chunk1.choices = [MagicMock(delta=MagicMock(content="你"))]
        chunk2 = MagicMock()
        chunk2.choices = [MagicMock(delta=MagicMock(content="好！"))]
        mock_client.chat.completions.create.return_value = [chunk1, chunk2]

        results = list(engine.chat_stream("hello", []))
        text_items = [r for r in results if r["type"] == "text"]
        assert len(text_items) == 2
        assert text_items[0]["content"] == "你"
        assert text_items[1]["content"] == "好！"


def test_extract_sticker_tags():
    """提取贴纸标记。"""
    from core.engine import ChatEngine
    text, ids = ChatEngine._extract_sticker_tags("哈哈哈 [sticker:happy_1] [sticker:sad_2]")
    assert text == "哈哈哈"
    assert ids == ["happy_1", "sad_2"]


def test_extract_sticker_tags_no_tags():
    """无贴纸标记时返回原文。"""
    from core.engine import ChatEngine
    text, ids = ChatEngine._extract_sticker_tags("普通回复")
    assert text == "普通回复"
    assert ids == []


def test_rag_degradation():
    """RAG 降级逻辑：连续 3 次失败后进入降级。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, _ = _create_test_engine(tmpdir)

        assert not engine._is_rag_degraded()

        engine._rag_failures = 3
        assert engine._is_rag_degraded()


def test_build_system_prompt_includes_skill():
    """system prompt 包含 SKILL.md 内容。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, _ = _create_test_engine(tmpdir)
        prompt = engine._build_system_prompt()
        assert "测试人格" in prompt
        assert "可用图片表情包" in prompt


def test_build_system_prompt_with_rag():
    """system prompt 包含 RAG 检索结果。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, _ = _create_test_engine(tmpdir)
        rag_results = [
            {"display_text": "ta 真实说过的话", "score": 0.9}
        ]
        prompt = engine._build_system_prompt(rag_results=rag_results)
        assert "ta 真实说过的话" in prompt
        assert "潜意识层" in prompt


def test_build_system_prompt_rag_below_threshold():
    """低于阈值的 RAG 结果不出现在 prompt 中。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, _ = _create_test_engine(tmpdir)
        from config import RAG_THRESHOLD
        rag_results = [
            {"display_text": "低分结果", "score": RAG_THRESHOLD - 0.1}
        ]
        prompt = engine._build_system_prompt(rag_results=rag_results)
        assert "低分结果" not in prompt


def test_chat_with_history():
    """chat 方法传递历史消息。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        engine, mock_client = _create_test_engine(tmpdir)

        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="继续聊"))]
        mock_response.usage = MagicMock(prompt_tokens=20, completion_tokens=5)
        mock_client.chat.completions.create.return_value = mock_response

        history = [
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的回答"},
        ]
        reply, _, _ = engine.chat("新的问题", history)
        assert reply == "继续聊"

        # 验证传入的消息包含历史
        call_args = mock_client.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]
        # system + 2 history + 1 user = 4
        assert len(messages) == 4
