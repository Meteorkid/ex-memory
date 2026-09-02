"""表达指纹：把「像不像 ta」变成可测量的数字（FR-060）。

现有 evals 评的是检索准不准、生成有没有编造事实，评不出像不像这个人。
这些指标全部是纯统计，成本为零、结果可复现，所以能进 CI 当发布门禁。
"""

import pytest

from evals.fingerprint import (
    describe,
    distance,
    drift,
    fingerprint,
    target_texts_from_corpus,
)

# 微信式短句：短、无句末标点、语气词多
WECHAT_STYLE = [
    "在吗",
    "刚看到",
    "今天好累",
    "嗯嗯",
    "哈哈哈",
    "好呀",
    "我先睡了",
    "明天见",
    "你吃饭了没",
    "困死了",
]
# 同一个人的另一批话
WECHAT_STYLE_ALT = [
    "在不",
    "刚看见",
    "今天有点累",
    "嗯",
    "哈哈",
    "好啊",
    "先睡啦",
    "晚安",
    "吃了吗",
    "好困",
]
# 通用 AI 腔：长句、书面语、句末规整
AI_STYLE = [
    "您好，我理解您的感受。这是一个非常值得探讨的话题，让我们一起来分析其中的原因。",
    "根据您所描述的情况，我认为可以从以下几个方面来考虑这个问题。",
    "首先需要明确的是，情绪的产生往往有其深层次的心理机制。",
    "希望我的回答对您有所帮助，如果还有其他疑问欢迎随时提出。",
]


class TestDiscrimination:
    def test_same_style_scores_low(self):
        assert distance(fingerprint(WECHAT_STYLE), fingerprint(WECHAT_STYLE_ALT)) < 0.25

    def test_ai_speak_scores_high(self):
        """长对话里语气滑向通用 AI 腔是典型失败模式，指标必须能抓到。"""
        assert distance(fingerprint(WECHAT_STYLE), fingerprint(AI_STYLE)) > 0.5

    def test_identical_input_is_zero(self):
        assert distance(fingerprint(WECHAT_STYLE), fingerprint(WECHAT_STYLE)) == 0.0

    def test_distance_is_symmetric(self):
        a, b = fingerprint(WECHAT_STYLE), fingerprint(AI_STYLE)
        assert distance(a, b) == distance(b, a)

    def test_distance_is_bounded(self):
        assert 0.0 <= distance(fingerprint(WECHAT_STYLE), fingerprint(AI_STYLE)) <= 1.0


class TestFeatures:
    def test_captures_missing_end_punctuation(self):
        """微信里句末不打标点是很强的个人特征。"""
        assert fingerprint(["在吗", "好的", "嗯"])["no_end_punct_rate"] == 1.0
        assert fingerprint(["在吗。", "好的。"])["no_end_punct_rate"] == 0.0

    def test_captures_emoji_rate(self):
        plain = fingerprint(["今天天气不错"])
        with_emoji = fingerprint(["今天天气不错😊😊"])
        assert with_emoji["emoji_rate"] > plain["emoji_rate"]

    def test_captures_filler_words(self):
        fp = fingerprint(["哈哈哈哈", "嗯嗯嗯"])
        summary = describe(fp)
        assert "哈哈" in summary["top_filler"] or "嗯" in summary["top_filler"]

    def test_multi_sentence_messages_are_detected(self):
        single = fingerprint(["就一句话"])
        multi = fingerprint(["第一句。第二句。第三句。"])
        assert multi["avg_sentences_per_message"] > single["avg_sentences_per_message"]

    def test_empty_input_is_handled(self):
        assert fingerprint([])["sample_size"] == 0
        assert fingerprint(["", "  "])["sample_size"] == 0


class TestDrift:
    def test_no_drift_within_consistent_style(self):
        assert drift(WECHAT_STYLE[:5], WECHAT_STYLE[5:]) < 0.3

    def test_drift_detected_when_style_shifts(self):
        """前 10 轮像 ta、后面滑向 AI 腔——这正是要拦住的。"""
        assert drift(WECHAT_STYLE, AI_STYLE) > 0.5

    def test_empty_side_raises_rather_than_silently_passing(self):
        with pytest.raises(ValueError):
            drift([], WECHAT_STYLE)


class TestCorpusExtraction:
    def test_only_target_messages_are_used(self):
        """混入用户自己的发言会把指纹拉平，测不出差异。"""
        messages = [
            {"content": "ta 说的", "is_target": True},
            {"content": "我说的", "is_target": False},
            {"content": "ta 又说", "is_target": True},
        ]
        assert target_texts_from_corpus(messages) == ["ta 说的", "ta 又说"]

    def test_can_include_everyone_when_asked(self):
        messages = [
            {"content": "a", "is_target": True},
            {"content": "b", "is_target": False},
        ]
        assert len(target_texts_from_corpus(messages, target_only=False)) == 2

    def test_empty_content_is_skipped(self):
        messages = [
            {"content": "", "is_target": True},
            {"content": None, "is_target": True},
        ]
        assert target_texts_from_corpus(messages) == []


class TestDeterminism:
    def test_same_input_gives_identical_result(self):
        """纯统计、可复现——这是它能当 CI 门禁的前提。"""
        assert fingerprint(WECHAT_STYLE) == fingerprint(WECHAT_STYLE)

    def test_no_llm_call_involved(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("表达指纹不该调用 LLM")

        monkeypatch.setattr("config.get_llm_client", boom)
        distance(fingerprint(WECHAT_STYLE), fingerprint(AI_STYLE))


class TestMirrorBaseline:
    def test_fingerprint_from_corpus_archive(self, tmp_path, monkeypatch):
        from core.corpus_store import append_messages
        from evals.fingerprint import fingerprint_for_mirror

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "fp").mkdir(parents=True)
        append_messages(
            "fp",
            [{"content": t, "is_target": True} for t in WECHAT_STYLE],
            source="wechat",
        )
        fp = fingerprint_for_mirror("fp")
        assert fp["sample_size"] == len(WECHAT_STYLE)
        assert fp["no_end_punct_rate"] == 1.0
