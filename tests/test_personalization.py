"""personalization 模块测试。"""

from core.personalization import analyze_user_style, calculate_relationship_temperature


class TestAnalyzeUserStyle:
    """用户风格分析。"""

    def test_basic_analysis(self):
        messages = [
            {"role": "user", "content": "今天好开心啊"},
            {"role": "user", "content": "哈哈哈太棒了"},
            {"role": "assistant", "content": "是呀"},
        ]
        result = analyze_user_style(messages)
        assert "common_words" in result
        assert "avg_message_length" in result
        assert "reply_speed" in result
        assert "emotional_tendency" in result

    def test_empty_messages(self):
        result = analyze_user_style([])
        assert result["common_words"] == []
        assert result["avg_message_length"] == 0
        assert result["emotional_tendency"] == "neutral"

    def test_positive_tendency(self):
        messages = [
            {"role": "user", "content": "开心高兴快乐幸福"},
            {"role": "user", "content": "哈哈太好了爱喜欢"},
            {"role": "user", "content": "开心开心开心"},
        ]
        result = analyze_user_style(messages)
        assert result["emotional_tendency"] == "positive"

    def test_negative_tendency(self):
        messages = [
            {"role": "user", "content": "难过伤心生气讨厌"},
            {"role": "user", "content": "烦哭失望"},
            {"role": "user", "content": "难过难过难过"},
        ]
        result = analyze_user_style(messages)
        assert result["emotional_tendency"] == "negative"

    def test_reply_speed_fast(self):
        messages = [{"role": "user", "content": "短消息"} for _ in range(5)]
        result = analyze_user_style(messages)
        assert result["reply_speed"] == "fast"

    def test_reply_speed_slow(self):
        messages = [{"role": "user", "content": "x" * 150} for _ in range(5)]
        result = analyze_user_style(messages)
        assert result["reply_speed"] == "slow"

    def test_only_user_messages_counted(self):
        messages = [
            {"role": "user", "content": "开心"},
            {"role": "assistant", "content": "难过难过难过难过难过"},
        ]
        result = analyze_user_style(messages)
        # 只统计 user 消息
        assert result["emotional_tendency"] == "positive"


class TestCalculateRelationshipTemperature:
    """关系温度计算。"""

    def test_basic_temperature(self):
        messages = [
            {"role": "user", "content": "你好", "created_at": "2024-01-01T10:00:00"},
            {"role": "assistant", "content": "你好呀", "created_at": "2024-01-01T10:01:00"},
        ]
        result = calculate_relationship_temperature("test", messages)
        assert "temperature" in result
        assert "level" in result
        assert "factors" in result
        assert 0 <= result["temperature"] <= 100

    def test_empty_messages(self):
        result = calculate_relationship_temperature("test", [])
        assert result["temperature"] == 50
        assert result["level"] == "warm"

    def test_hot_level(self):
        # 大量含爱意的消息
        messages = []
        for i in range(50):
            messages.append({"role": "user", "content": f"爱你宝贝想你 {i}", "created_at": f"2024-01-{i+1:02d}T10:00:00"})
            messages.append({"role": "assistant", "content": f"亲爱的也好爱你 {i}", "created_at": f"2024-01-{i+1:02d}T10:01:00"})
        result = calculate_relationship_temperature("test", messages)
        assert result["temperature"] >= 60

    def test_cold_level(self):
        # 少量无情感的消息
        messages = [
            {"role": "user", "content": "哦", "created_at": "2024-01-01T10:00:00"},
        ]
        result = calculate_relationship_temperature("test", messages)
        assert result["temperature"] < 60

    def test_factors_present(self):
        messages = [
            {"role": "user", "content": "你好", "created_at": "2024-01-01T10:00:00"},
        ]
        result = calculate_relationship_temperature("test", messages)
        factors = result["factors"]
        assert "frequency" in factors
        assert "emotion" in factors
        assert "balance" in factors
        assert "depth" in factors
        assert "time" in factors
