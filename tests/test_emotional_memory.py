"""emotional_memory 模块测试。"""

from core.emotional_memory import (
    extract_emotional_memories,
    get_memory_context,
)


class TestExtractEmotionalMemories:
    """情感记忆提取。"""

    def test_basic_extraction(self):
        messages = [
            {"role": "user", "content": "宝贝我想你了", "created_at": "2024-01-01T10:00:00"},
            {"role": "assistant", "content": "亲爱的我也想你", "created_at": "2024-01-01T10:01:00"},
        ]
        result = extract_emotional_memories(messages)
        assert "important_dates" in result
        assert "shared_experiences" in result
        assert "emotional_milestones" in result
        assert "pet_names" in result

    def test_pet_names_extraction(self):
        messages = [
            {"role": "assistant", "content": "宝贝你真好", "created_at": ""},
            {"role": "assistant", "content": "亲爱的我爱你", "created_at": ""},
        ]
        result = extract_emotional_memories(messages)
        assert "宝贝" in result["pet_names"]
        assert "亲爱的" in result["pet_names"]

    def test_important_dates_extraction(self):
        messages = [
            {"role": "user", "content": "生日快乐！", "created_at": "2024-01-15T10:00:00"},
            {"role": "user", "content": "今天是我们的纪念日", "created_at": "2024-02-14T10:00:00"},
        ]
        result = extract_emotional_memories(messages)
        assert len(result["important_dates"]) >= 2

    def test_shared_experiences_extraction(self):
        messages = [
            {"role": "user", "content": "还记得那次在公园吗", "created_at": ""},
            {"role": "user", "content": "第一次见面的时候", "created_at": ""},
        ]
        result = extract_emotional_memories(messages)
        assert len(result["shared_experiences"]) >= 2

    def test_milestone_extraction(self):
        messages = [
            {"role": "user", "content": "第一次说爱你的时候", "created_at": ""},
        ]
        result = extract_emotional_memories(messages)
        assert len(result["emotional_milestones"]) >= 1

    def test_empty_messages(self):
        result = extract_emotional_memories([])
        assert result["important_dates"] == []
        assert result["shared_experiences"] == []
        assert result["emotional_milestones"] == []
        assert result["pet_names"] == []

    def test_limits(self):
        # 超过限制数量
        messages = [{"role": "user", "content": "生日快乐", "created_at": ""} for _ in range(50)]
        result = extract_emotional_memories(messages)
        assert len(result["important_dates"]) <= 20
        assert len(result["shared_experiences"]) <= 30
        assert len(result["emotional_milestones"]) <= 10
        assert len(result["pet_names"]) <= 5


class TestGetMemoryContext:
    """记忆上下文生成。"""

    def test_with_memories(self, tmp_path, monkeypatch):
        # Mock get_ex_dir
        import config
        monkeypatch.setattr(config, "get_ex_dir", lambda slug: tmp_path)

        # 创建记忆文件
        import json
        memories = {
            "important_dates": [{"content": "生日快乐", "timestamp": "", "type": "birthday", "keyword": "生日"}],
            "shared_experiences": [{"content": "那次在公园", "timestamp": "", "type": "memory"}],
            "emotional_milestones": [],
            "pet_names": ["宝贝", "亲爱的"],
        }
        (tmp_path / "emotional_memories.json").write_text(
            json.dumps(memories, ensure_ascii=False), encoding="utf-8"
        )

        context = get_memory_context("test")
        assert "重要记忆" in context
        assert "共同经历" in context
        assert "常用称呼" in context

    def test_without_memories(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "get_ex_dir", lambda slug: tmp_path)

        context = get_memory_context("test")
        assert context == ""
