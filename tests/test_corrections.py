"""纠正记录的结构化沉淀（FR-070 / D-19）。

原实现是无上限追加的流水账，全量注入 prompt 会一直涨——而它标着
「优先级最高」，被上下文预算挤掉的反而是最该保留的东西。
"""

import pytest

from core.corrections import (
    MAX_ENTRIES,
    add,
    as_eval_cases,
    load,
    prompt_section,
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    (tmp_path / "exes" / "c").mkdir(parents=True)
    return tmp_path


class TestAdd:
    def test_first_entry(self, env):
        entry = add("c", "ta 不会说「亲爱的」")
        assert entry["count"] == 1
        assert len(load("c")) == 1

    def test_empty_rejected(self, env):
        with pytest.raises(ValueError):
            add("c", "   ")

    def test_same_topic_merges_instead_of_duplicating(self, env):
        """反复纠正同一件事说明它更重要，而不是该占更多篇幅。"""
        add("c", "ta 不会说亲爱的")
        entry = add("c", "ta 不会说「亲爱的」。")
        assert entry["count"] == 2
        assert len(load("c")) == 1

    def test_different_topics_are_separate(self, env):
        add("c", "ta 不用感叹号")
        add("c", "ta 从不主动道歉")
        assert len(load("c")) == 2


class TestPruning:
    def test_capped_at_max_entries(self, env):
        for i in range(MAX_ENTRIES + 15):
            add("c", f"第{i}条完全不同的纠正内容")
        assert len(load("c")) <= MAX_ENTRIES

    def test_frequently_corrected_entries_survive(self, env):
        """高频纠正是更重要的信号，淘汰时要留下。"""
        add("c", "这条很重要要保留")
        for _ in range(5):
            add("c", "这条很重要要保留")
        for i in range(MAX_ENTRIES + 20):
            add("c", f"低频第{i}条")
        contents = [e["content"] for e in load("c")]
        assert "这条很重要要保留" in contents

    def test_entries_stay_in_chronological_order(self, env):
        for i in range(5):
            add("c", f"第{i}条")
        entries = load("c")
        assert entries == sorted(entries, key=lambda e: e["created_at"])


class TestPromptSection:
    def test_empty_costs_no_tokens(self, env):
        assert prompt_section("c") == ""

    def test_contains_entries_and_priority_marker(self, env):
        add("c", "ta 不用感叹号")
        section = prompt_section("c")
        assert "优先级最高" in section
        assert "ta 不用感叹号" in section

    def test_repeat_count_is_emphasized(self, env):
        for _ in range(3):
            add("c", "ta 从不叫全名")
        assert "被纠正过 3 次" in prompt_section("c")

    def test_prompt_is_bounded(self, env):
        from core.corrections import MAX_IN_PROMPT

        for i in range(MAX_ENTRIES):
            add("c", f"纠正条目编号{i}")
        assert prompt_section("c").count("\n- ") <= MAX_IN_PROMPT


class TestLegacyMigration:
    def test_structured_legacy_file_is_migrated(self, env):
        legacy = env / "exes" / "c" / "corrections.md"
        legacy.write_text(
            "# 纠正记录\n\n### Correction #1 — 2026-01-01\n第一条\n\n---\n"
            "### Correction #2 — 2026-01-02\n第二条\n\n---\n",
            encoding="utf-8",
        )
        entries = load("c")
        assert len(entries) == 2
        assert "第一条" in entries[0]["content"]

    def test_unstructured_legacy_file_is_not_lost(self, env):
        """手工编辑过的旧文件没有表头，不能因为认不出格式就丢掉。"""
        legacy = env / "exes" / "c" / "corrections.md"
        legacy.write_text("ta 说话很简短，别写长句", encoding="utf-8")
        entries = load("c")
        assert len(entries) == 1
        assert "别写长句" in entries[0]["content"]

    def test_migration_is_persisted_not_repeated(self, env):
        legacy = env / "exes" / "c" / "corrections.md"
        legacy.write_text("旧内容", encoding="utf-8")
        load("c")
        legacy.unlink()
        # 迁移后即使旧文件没了，内容依然在
        assert len(load("c")) == 1

    def test_no_legacy_file_is_fine(self, env):
        assert load("c") == []


class TestEvalCases:
    def test_corrections_become_eval_cases(self, env):
        """纠正收敛率要能测，前提是纠正本身是结构化的。"""
        add("c", "ta 不会主动道歉")
        for _ in range(2):
            add("c", "ta 不用感叹号")
        cases = as_eval_cases("c")
        assert len(cases) == 2
        assert any(c["times_corrected"] == 2 for c in cases)


class TestEngineIntegration:
    def test_engine_reads_structured_corrections(self, env, monkeypatch):
        from unittest.mock import patch

        ex_dir = env / "exes" / "eng"
        ex_dir.mkdir(parents=True)
        (ex_dir / "SKILL.md").write_text("# 人格", encoding="utf-8")
        (ex_dir / "sessions").mkdir()
        add("eng", "ta 从不用句号")

        with patch(
            "core.engine.get_llm_config",
            return_value={
                "model": "m",
                "temperature": 0.8,
                "top_p": 0.9,
                "frequency_penalty": 0.6,
                "max_tokens": 100,
            },
        ):
            from core.engine import ChatEngine

            engine = ChatEngine("eng", vector_store=None, embedder=None)
        assert "ta 从不用句号" in engine._build_system_prompt()
