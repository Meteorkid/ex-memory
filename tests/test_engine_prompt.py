"""ChatEngine system prompt 截断测试。"""

import pytest
from core.engine import ChatEngine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    slug = "prompttest"
    ex = tmp_path / slug
    ex.mkdir()
    (ex / "SKILL.md").write_text("# skill", encoding="utf-8")
    (ex / "corrections.md").write_text("纠正内容", encoding="utf-8")
    monkeypatch.setattr("core.engine.get_ex_dir", lambda s: tmp_path / s)
    monkeypatch.setattr("config.get_ex_dir", lambda s: tmp_path / s)
    monkeypatch.setattr("config.LLM_MAX_CONTEXT_CHARS", 100)
    eng = ChatEngine(slug, vector_store=None, embedder=None)
    eng.session_summaries = ["摘要" * 50, "摘要2" * 50, "摘要3" * 50]
    return eng


def test_truncation_keeps_corrections(engine):
    prompt = engine._build_system_prompt(rag_results=[])
    assert "纠正内容" in prompt
    assert "可用图片表情包" in prompt
