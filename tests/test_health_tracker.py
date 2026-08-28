"""health_tracker 模块测试。"""

import time
from core.health_tracker import HealthTracker, HEALTH_TIPS, MINDFUL_MESSAGES


class TestHealthTracker:
    """HealthTracker 类测试。"""

    def setup_method(self):
        self.tracker = HealthTracker()

    def test_start_session(self):
        self.tracker.start_session(1)
        assert 1 in self.tracker._sessions

    def test_should_remind_not_started(self):
        assert self.tracker.should_remind(999) is False

    def test_should_remind_too_soon(self):
        self.tracker.start_session(1)
        assert self.tracker.should_remind(1) is False

    def test_should_remind_after_interval(self):
        # 模拟 31 分钟前登录
        self.tracker._sessions[1] = time.time() - 1860
        assert self.tracker.should_remind(1) is True

    def test_should_not_double_remind(self):
        # 模拟已提醒过
        self.tracker._sessions[1] = time.time() - 1860
        self.tracker._last_reminder[1] = time.time() - 60  # 1分钟前提醒过
        assert self.tracker.should_remind(1) is False

    def test_get_usage_stats(self):
        self.tracker.start_session(1)
        stats = self.tracker.get_usage_stats(1)
        assert "session_duration_minutes" in stats
        assert "session_start" in stats
        assert stats["session_duration_minutes"] >= 0

    def test_get_mindful_message(self):
        msg = self.tracker.get_mindful_message()
        assert msg in MINDFUL_MESSAGES

    def test_get_health_tip(self):
        tip = self.tracker.get_health_tip()
        assert tip in HEALTH_TIPS

    def test_end_session(self):
        self.tracker.start_session(1)
        self.tracker.end_session(1)
        assert 1 not in self.tracker._sessions

    def test_multiple_users(self):
        self.tracker.start_session(1)
        self.tracker.start_session(2)
        self.tracker.end_session(1)
        assert 1 not in self.tracker._sessions
        assert 2 in self.tracker._sessions
