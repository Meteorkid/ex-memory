"""emotion_tracker 模块测试。"""

from core.emotion_tracker import (
    analyze_sentiment,
    analyze_history,
    generate_emotion_curve,
    get代表性原话,
)


class TestAnalyzeSentiment:
    """单条消息情感分析。"""

    def test_positive_message(self):
        result = analyze_sentiment("好开心啊，太棒了！")
        assert result["label"] == "positive"
        assert result["score"] > 0
        assert result["positive"] > 0

    def test_negative_message(self):
        result = analyze_sentiment("好难过，心都碎了")
        assert result["label"] == "negative"
        assert result["score"] < 0
        assert result["negative"] > 0

    def test_neutral_message(self):
        result = analyze_sentiment("今天星期三")
        assert result["label"] == "neutral"
        assert result["score"] == 0.0

    def test_mixed_message(self):
        result = analyze_sentiment("开心但又有点难过")
        assert result["label"] in ("positive", "negative", "neutral")
        assert result["positive"] > 0
        assert result["negative"] > 0

    def test_empty_message(self):
        result = analyze_sentiment("")
        assert result["label"] == "neutral"
        assert result["score"] == 0.0

    def test_score_range(self):
        for text in ["爱爱爱爱爱", "讨厌讨厌讨厌", "哦"]:
            result = analyze_sentiment(text)
            assert -1.0 <= result["score"] <= 1.0


class TestAnalyzeHistory:
    """对话历史情感分析。"""

    def test_all_positive(self):
        history = [
            {"role": "user", "content": "好开心"},
            {"role": "assistant", "content": "我也开心"},
        ]
        result = analyze_history(history)
        assert result["overall"]["label"] == "positive"
        assert result["message_count"] == 2

    def test_all_negative(self):
        history = [
            {"role": "user", "content": "好难过"},
            {"role": "assistant", "content": "伤心"},
        ]
        result = analyze_history(history)
        assert result["overall"]["label"] == "negative"

    def test_empty_history(self):
        result = analyze_history([])
        assert result["message_count"] == 0
        assert result["overall"]["score"] == 0.0

    def test_user_assistant_separation(self):
        history = [
            {"role": "user", "content": "开心"},
            {"role": "assistant", "content": "难过"},
        ]
        result = analyze_history(history)
        assert result["user_sentiment"]["label"] == "positive"
        assert result["assistant_sentiment"]["label"] == "negative"

    def test_counts(self):
        history = [
            {"role": "user", "content": "开心"},
            {"role": "user", "content": "难过"},
            {"role": "assistant", "content": "一般"},
        ]
        result = analyze_history(history)
        assert result["positive_count"] >= 1
        assert result["negative_count"] >= 1
        assert result["neutral_count"] >= 1


class TestGenerateEmotionCurve:
    """情感曲线生成。"""

    def test_basic_curve(self):
        history = [{"role": "user", "content": f"消息{i}"} for i in range(25)]
        curve = generate_emotion_curve(history, bucket_size=10)
        assert len(curve) == 3  # 25条消息，每10条一组
        assert curve[0]["bucket"] == 0
        assert curve[0]["count"] == 10

    def test_empty_history(self):
        assert generate_emotion_curve([]) == []

    def test_small_history(self):
        history = [{"role": "user", "content": "开心"}]
        curve = generate_emotion_curve(history, bucket_size=10)
        assert len(curve) == 1
        assert curve[0]["count"] == 1


class TestGet代表性原话:
    """代表性原话检索。"""

    def test_find_keyword(self):
        history = [
            {"content": "我好想你啊"},
            {"content": "今天天气不错"},
            {"content": "真的好想你"},
        ]
        results = get代表性原话(history, "想你")
        assert len(results) == 2

    def test_max_results(self):
        history = [{"content": f"想你{i}"} for i in range(10)]
        results = get代表性原话(history, "想你", max_results=3)
        assert len(results) <= 3

    def test_no_match(self):
        history = [{"content": "今天天气不错"}]
        results = get代表性原话(history, "想你")
        assert len(results) == 0
