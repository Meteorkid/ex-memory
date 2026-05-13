"""共享 fixtures。"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sample_wechat_messages():
    """模拟微信聊天记录。"""
    return [
        {"sender": "小明", "content": "今天天气真好", "timestamp": "2024-01-01 10:00", "is_target": True},
        {"sender": "我", "content": "是啊，要不要出去走走", "timestamp": "2024-01-01 10:01", "is_target": False},
        {"sender": "小明", "content": "好呀好呀！去哪里？", "timestamp": "2024-01-01 10:02", "is_target": True},
        {"sender": "我", "content": "去公园吧", "timestamp": "2024-01-01 10:03", "is_target": False},
        {"sender": "小明", "content": "嗯嗯，我最喜欢公园了", "timestamp": "2024-01-01 10:04", "is_target": True},
        {"sender": "小明", "content": "等我换个衣服", "timestamp": "2024-01-01 10:05", "is_target": True},
        {"sender": "我", "content": "好的不着急", "timestamp": "2024-01-01 10:06", "is_target": False},
        {"sender": "小明", "content": "好啦走吧！", "timestamp": "2024-01-01 10:15", "is_target": True},
    ]


@pytest.fixture
def sample_target_heavy_messages():
    """目标发言占多数的消息。"""
    msgs = []
    for i in range(10):
        msgs.append({"sender": "小明", "content": f"这是ta的消息{i}", "timestamp": f"10:{i:02d}", "is_target": True})
    for i in range(3):
        msgs.append({"sender": "我", "content": f"我的回复{i}", "timestamp": f"10:{10+i:02d}", "is_target": False})
    return msgs
