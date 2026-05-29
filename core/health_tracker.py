"""情感健康追踪：使用时长提醒、正念引导、健康贴士。"""

import time
import random
from datetime import datetime

# ── 健康贴士库 ──
HEALTH_TIPS = [
    "深呼吸三次，感受当下的平静。",
    "记忆是珍贵的，但当下更值得珍惜。",
    "适当休息一下，看看窗外的风景。",
    "和真实的朋友聊聊天，感受真实的温暖。",
    "运动 10 分钟，让身体和心情都好起来。",
    "写下今天发生的三件好事。",
    "给自己泡一杯喜欢的饮品。",
    "听听喜欢的音乐，让心情放松。",
    "整理一下房间，整洁的环境让心情更舒畅。",
    "给未来的自己写一封信。",
]

MINDFUL_MESSAGES = [
    "这只是记忆，不是现实。愿你安好。",
    "回忆很美，但生活还在继续。",
    "放下过去，才能拥抱未来。",
    "你值得被真实地爱着。",
    "愿你在现实中找到温暖。",
]


class HealthTracker:
    """追踪用户使用习惯，提供健康提醒。"""

    def __init__(self):
        self._sessions: dict[int, float] = {}  # user_id -> login_timestamp
        self._last_reminder: dict[int, float] = {}  # user_id -> last_reminder_timestamp
        self._reminder_interval = 1800  # 30 分钟

    def start_session(self, user_id: int):
        """记录用户登录时间。"""
        self._sessions[user_id] = time.time()

    def should_remind(self, user_id: int) -> bool:
        """检查是否应该显示使用时长提醒。"""
        if user_id not in self._sessions:
            return False

        now = time.time()
        session_duration = now - self._sessions[user_id]
        last_reminder = self._last_reminder.get(user_id, 0)

        # 超过 30 分钟 且 距离上次提醒超过 30 分钟
        if session_duration > self._reminder_interval and (now - last_reminder) > self._reminder_interval:
            self._last_reminder[user_id] = now
            return True
        return False

    def get_usage_stats(self, user_id: int) -> dict:
        """获取用户使用统计。"""
        now = time.time()
        session_start = self._sessions.get(user_id, now)
        session_duration = int(now - session_start)

        return {
            "session_duration_minutes": round(session_duration / 60, 1),
            "session_start": datetime.fromtimestamp(session_start).isoformat(),
            "current_time": datetime.now().isoformat(),
        }

    def get_mindful_message(self) -> str:
        """获取正念引导消息。"""
        return random.choice(MINDFUL_MESSAGES)

    def get_health_tip(self) -> str:
        """获取随机健康贴士。"""
        return random.choice(HEALTH_TIPS)


# 全局实例
health_tracker = HealthTracker()
