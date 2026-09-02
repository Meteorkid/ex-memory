"""表达风格塑形：分条、延迟、不完美、相对时间（FR-062 ~ FR-064、FR-067）。

做法是从 ta 的真实语料统计出习惯并作为显式指令注入 prompt，
而不是调高 temperature 制造随机——随机性不等于人性。
"""

from datetime import datetime, timedelta

from core.persona_style import (
    SPLIT_MARKER,
    StyleProfile,
    profile_from_corpus,
    relative_time_hint,
    reply_delay_seconds,
    split_reply,
    style_instructions,
)

TERSE = StyleProfile(
    avg_message_chars=8,
    messages_per_reply=1.1,
    no_end_punct_rate=0.9,
    emoji_rate=0.001,
    top_fillers=["嗯", "哈哈"],
    sample_size=200,
)
VERBOSE = StyleProfile(
    avg_message_chars=45,
    messages_per_reply=2.5,
    no_end_punct_rate=0.1,
    emoji_rate=0.05,
    top_fillers=["那个"],
    sample_size=200,
)


class TestSplitReply:
    def test_model_provided_separators_win(self):
        """模型知道哪里断句自然，优先用它给的。"""
        assert split_reply(f"在吗{SPLIT_MARKER}刚看到{SPLIT_MARKER}今天好累") == [
            "在吗",
            "刚看到",
            "今天好累",
        ]

    def test_short_reply_is_not_chopped(self):
        """切错位置比不切更出戏。"""
        assert split_reply("嗯嗯知道了") == ["嗯嗯知道了"]

    def test_long_reply_falls_back_to_sentence_boundaries(self):
        text = "今天真的好累啊。刚下班回到家。晚饭都还没吃呢。就想躺着不动。"
        parts = split_reply(text, TERSE)
        assert len(parts) > 1
        assert all(p.strip() for p in parts)

    def test_empty_input(self):
        assert split_reply("") == []
        assert split_reply("   ") == []

    def test_blank_segments_are_dropped(self):
        assert split_reply(f"在吗{SPLIT_MARKER}{SPLIT_MARKER}好") == ["在吗", "好"]

    def test_non_profile_object_does_not_crash(self):
        """调用方可能传进来别的对象，拿它的属性做数值比较会炸。"""
        from unittest.mock import MagicMock

        assert split_reply("嗯", MagicMock()) == ["嗯"]


class TestReplyDelay:
    def test_deep_night_is_slower_than_daytime(self):
        """真人深夜可能在睡，不会秒回。"""
        night = reply_delay_seconds(0, "今天好累", TERSE, datetime(2026, 9, 2, 3, 0))
        evening = reply_delay_seconds(0, "今天好累", TERSE, datetime(2026, 9, 2, 20, 0))
        # 有抖动，多取几次比均值
        nights = [
            reply_delay_seconds(0, "今天好累", TERSE, datetime(2026, 9, 2, 3, 0))
            for _ in range(20)
        ]
        evenings = [
            reply_delay_seconds(0, "今天好累", TERSE, datetime(2026, 9, 2, 20, 0))
            for _ in range(20)
        ]
        assert sum(nights) / 20 > sum(evenings) / 20
        assert night > 0 and evening > 0

    def test_workday_daytime_is_slower(self):
        work = [
            reply_delay_seconds(0, "在", TERSE, datetime(2026, 9, 2, 14, 0))
            for _ in range(20)
        ]
        weekend = [
            reply_delay_seconds(0, "在", TERSE, datetime(2026, 9, 6, 14, 0))
            for _ in range(20)
        ]
        assert sum(work) / 20 > sum(weekend) / 20

    def test_longer_text_takes_longer(self):
        short = [reply_delay_seconds(1, "嗯", TERSE) for _ in range(20)]
        long = [
            reply_delay_seconds(1, "今天真的好累啊我都不想动了" * 2, TERSE)
            for _ in range(20)
        ]
        assert sum(long) / 20 > sum(short) / 20

    def test_first_message_waits_longer_than_followups(self):
        first = [reply_delay_seconds(0, "嗯", TERSE) for _ in range(30)]
        later = [reply_delay_seconds(2, "嗯", TERSE) for _ in range(30)]
        assert sum(first) / 30 > sum(later) / 30

    def test_delay_is_bounded(self):
        assert reply_delay_seconds(0, "很长的话" * 200, VERBOSE) <= 12.0

    def test_jitter_prevents_identical_intervals(self):
        """完全一致的间隔本身就很机器。"""
        values = {reply_delay_seconds(1, "嗯嗯", TERSE) for _ in range(30)}
        assert len(values) > 1


class TestStyleInstructions:
    def test_always_asks_for_multi_message(self):
        assert SPLIT_MARKER in style_instructions(None)
        assert SPLIT_MARKER in style_instructions(TERSE)

    def test_terse_profile_asks_for_short_messages(self):
        text = style_instructions(TERSE)
        assert "说话很短" in text
        assert "不打标点" in text

    def test_low_emoji_profile_forbids_emoji(self):
        assert "不用 emoji" in style_instructions(TERSE)

    def test_high_emoji_profile_allows_emoji(self):
        assert "会用 emoji" in style_instructions(VERBOSE)

    def test_fillers_are_surfaced(self):
        assert "嗯" in style_instructions(TERSE)

    def test_imperfection_is_explicit(self):
        """有求必应是第三个出戏点。"""
        text = style_instructions(TERSE)
        assert "敷衍" in text or "不必有求必应" in text

    def test_no_profile_still_gives_wechat_guidance(self):
        assert "15 字" in style_instructions(None)


class TestProfileFromCorpus:
    def test_small_sample_returns_none(self, tmp_path, monkeypatch):
        """样本太少统计不出稳定风格，宁可不注入也不要注入错的。"""
        from core.corpus_store import append_messages

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "few").mkdir(parents=True)
        append_messages(
            "few", [{"content": "在吗", "is_target": True}] * 5, source="wechat"
        )
        assert profile_from_corpus("few") is None

    def test_derives_profile_from_enough_samples(self, tmp_path, monkeypatch):
        from core.corpus_store import append_messages

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "many").mkdir(parents=True)
        append_messages(
            "many",
            [{"content": "在吗", "is_target": True} for _ in range(30)],
            source="wechat",
        )
        profile = profile_from_corpus("many")
        assert profile is not None
        assert profile.is_terse is True
        assert profile.sample_size == 30

    def test_ignores_user_own_messages(self, tmp_path, monkeypatch):
        from core.corpus_store import append_messages

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        (tmp_path / "exes" / "mixed").mkdir(parents=True)
        append_messages(
            "mixed",
            [{"content": "很长的一段话" * 20, "is_target": False} for _ in range(50)]
            + [{"content": "嗯", "is_target": True} for _ in range(25)],
            source="wechat",
        )
        profile = profile_from_corpus("mixed")
        assert profile.is_terse is True, "混入了用户自己的发言，指纹被拉平了"


class TestRelativeTime:
    """绝对时间感知此前已有；缺的是相对时间——「好久没聊了」需要知道间隔。"""

    NOW = datetime(2026, 9, 2, 12, 0)

    def _hint(self, hours):
        return relative_time_hint(
            (self.NOW - timedelta(hours=hours)).isoformat(), self.NOW
        )

    def test_first_conversation(self):
        assert "第一次" in relative_time_hint(None)

    def test_just_now(self):
        assert "刚刚" in self._hint(0.05)

    def test_minutes(self):
        assert "分钟" in self._hint(0.5)

    def test_hours(self):
        assert "小时" in self._hint(5)

    def test_yesterday(self):
        assert "昨天" in self._hint(24)

    def test_days(self):
        assert "5 天" in self._hint(24 * 5)

    def test_months(self):
        assert "个多月" in self._hint(24 * 60)

    def test_over_a_year(self):
        assert "一年多" in self._hint(24 * 400)

    def test_malformed_timestamp_is_silent(self):
        assert relative_time_hint("不是时间") == ""

    def test_future_timestamp_is_silent(self):
        future = (self.NOW + timedelta(hours=5)).isoformat()
        assert relative_time_hint(future, self.NOW) == ""


class TestPromptIntegration:
    def test_style_and_relative_time_appear_in_prompt(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        ex_dir = tmp_path / "exes" / "sty"
        ex_dir.mkdir(parents=True)
        (ex_dir / "SKILL.md").write_text("# 人格", encoding="utf-8")
        (ex_dir / "sessions").mkdir()

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

            engine = ChatEngine("sty", vector_store=None, embedder=None)

        engine.last_seen_at = (datetime.now() - timedelta(days=3)).isoformat()
        prompt = engine._build_system_prompt()
        assert SPLIT_MARKER in prompt, "没有要求分条回复"
        assert "3 天" in prompt, "相对时间没进 prompt"

    def test_volatile_content_comes_after_stable(self, tmp_path, monkeypatch):
        """易变内容排在稳定内容之后，否则会把前缀缓存挤掉。"""
        from unittest.mock import patch

        monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
        ex_dir = tmp_path / "exes" / "ord"
        ex_dir.mkdir(parents=True)
        (ex_dir / "SKILL.md").write_text("# 人格", encoding="utf-8")
        (ex_dir / "sessions").mkdir()

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

            engine = ChatEngine("ord", vector_store=None, embedder=None)

        prompt = engine._build_system_prompt()
        assert prompt.index("表达方式") < prompt.index("时间感知")
