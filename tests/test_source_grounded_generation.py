"""新聊天导入只生成有来源的记忆与表达；保留旧镜像的既有行为。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _completion(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


@pytest.mark.parametrize("kind", ["memory", "persona"])
def test_source_grounded_builders_send_evidence_templates_to_client(kind, monkeypatch):
    from pipeline import memory_builder, persona_builder

    module = memory_builder if kind == "memory" else persona_builder
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _completion("原话证据：小林说周六交实验报告；关系未知。"),
        _completion("# 有来源的记录"),
    ]
    monkeypatch.setattr(module, "get_llm_client", lambda: client)
    monkeypatch.setattr(module, "get_llm_config", lambda: {"model": "offline-test"})
    builder = getattr(module, f"build_{kind}")
    material = "小林（目标人物）：周六交实验报告。\n同学（其他说话人）：我负责代码。"
    vector = MagicMock()
    vector.search_target_only.return_value = []
    kwargs = {"vector_store": vector, "embedder": object()} if kind == "persona" else {}
    assert builder("student", material, source_grounded=True, **kwargs) == "# 有来源的记录"
    calls = client.chat.completions.create.call_args_list
    assert len(calls) == 2
    for call in calls:
        prompt = "\n".join(message["content"] for message in call.kwargs["messages"])
        assert "未知" in prompt and "证据" in prompt
        assert "周六交实验报告" in prompt
        assert "前任" not in prompt and "分手" not in prompt and "MBTI" not in prompt
    if kind == "persona":
        queries = " ".join(call.args[0] for call in vector.search_target_only.call_args_list)
        assert "吃醋" not in queries and "撒娇" not in queries and "占有欲" not in queries
        final_prompt = calls[-1].kwargs["messages"][-1]["content"]
        assert "请基于分析结果推断" not in final_prompt


@pytest.mark.parametrize("kind", ["memory", "persona"])
def test_legacy_builder_keeps_original_templates(kind, monkeypatch):
    from pipeline import memory_builder, persona_builder

    module = memory_builder if kind == "memory" else persona_builder
    client = MagicMock()
    client.chat.completions.create.side_effect = [_completion("分析"), _completion("旧输出")]
    monkeypatch.setattr(module, "get_llm_client", lambda: client)
    monkeypatch.setattr(module, "get_llm_config", lambda: {"model": "offline-test"})
    assert getattr(module, f"build_{kind}")("old", "旧材料") == "旧输出"
    for index, suffix in enumerate(("analyzer", "builder")):
        expected = (module.PROMPTS_DIR / f"{kind}_{suffix}.md").read_text()
        assert client.chat.completions.create.call_args_list[index].kwargs["messages"][0]["content"] == expected


@pytest.mark.parametrize("stage", [None, "source", "unexpected"])
def test_new_mirror_actual_engine_prompt_is_neutral_and_identifies_simulation(stage, monkeypatch):
    from core.mirror_store import mirror_store
    from core.engine import ChatEngine
    from pipeline.skill_combiner import write_skill
    import core.engine as engine_module

    monkeypatch.setattr(engine_module, "get_llm_config", lambda: {
        "model": "offline", "temperature": 0, "top_p": 1,
        "frequency_penalty": 0, "max_tokens": 64,
    })
    store = mirror_store("student", 1)
    meta = {"name": "同学", "creation_source": "chat_import", "owner_user_id": 1}
    if stage:
        meta["stage"] = stage
    store.write_json("meta.json", meta)
    store.write_text("memory.md", "原话证据：周六交实验报告。\n关系：未知。")
    store.write_text("persona.md", "原话：好呀。\n推断：无。\n未知：其他表达偏好。")
    write_skill("student", owner=1)
    engine = ChatEngine("student", owner=1)
    prompt = engine._build_system_prompt()
    assert engine.relationship_stage == "source"
    assert "AI 模拟" in prompt and "原话" in prompt and "未知" in prompt
    for forbidden in ("热恋期", "频繁表达爱意", "前任", "不是 AI 助手"):
        assert forbidden not in prompt
    assert "周六交实验报告" in prompt


def test_legacy_skill_rules_remain_unchanged():
    from core.mirror_store import mirror_store
    from pipeline.skill_combiner import combine

    store = mirror_store("old", 1)
    store.write_json("meta.json", {"name": "旧人物", "owner_user_id": 1})
    store.write_text("memory.md", "旧记忆")
    store.write_text("persona.md", "旧画像")
    skill = combine("old", owner=1)
    assert "你是旧人物，不是 AI 助手" in skill
    assert "旧记忆" in skill and "旧画像" in skill
