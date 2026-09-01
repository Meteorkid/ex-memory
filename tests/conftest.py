"""共享 fixtures。"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolate_exes_dir(tmp_path, monkeypatch):
    """把镜像根目录统一指向临时目录，禁止测试碰真实 exes/。

    没有这道兜底时，任何一个漏打桩的用例都会写进用户的真实镜像：
    test_create_flow_api_no_llm_key 只 patch 了 get_llm_config，
    orchestrator 内部的 get_ex_dir("test") 就落到了真实 exes/test，
    把那里的 meta.json 覆盖掉。

    需要特定布局的用例仍可自行 monkeypatch config.EXES_DIR 覆盖本 fixture。
    """
    # 刻意不预建目录：多数用例把 EXES_DIR 指向 tmp_path 本身，这里若真建了
    # 目录，反而会被它们当成一个镜像扫描到。需要时由被测代码自己 mkdir。
    exes = tmp_path / "_isolated_exes"
    monkeypatch.setattr("config.EXES_DIR", exes)
    return exes


@pytest.fixture(autouse=True)
def isolate_shared_kv():
    """每个用例重置共享 KV。

    限流窗口是跨用例累积的：不重置的话跑到第 60 个请求就开始 429，
    失败原因还极难定位。默认用进程内后端，不依赖外部 Redis。
    """
    from core import kv

    kv.reset_for_tests()
    kv.configure("")
    yield
    kv.reset_for_tests()


@pytest.fixture(autouse=True)
def relax_registration_gates(monkeypatch):
    """测试默认关闭实名与年龄门槛。

    与既有的「测试里关掉登录限流」同理：绝大多数用例注册账号只是为了拿到
    身份，不该被准入流程拖累。门槛本身由 tests/test_registration_gates.py
    显式打开后专门验证。
    """
    monkeypatch.setattr("config.REQUIRE_PHONE_VERIFICATION", False)
    monkeypatch.setattr("config.REQUIRE_AGE_CONFIRMATION", False)


@pytest.fixture
def sample_wechat_messages():
    """模拟微信聊天记录。"""
    return [
        {
            "sender": "小明",
            "content": "今天天气真好",
            "timestamp": "2024-01-01 10:00",
            "is_target": True,
        },
        {
            "sender": "我",
            "content": "是啊，要不要出去走走",
            "timestamp": "2024-01-01 10:01",
            "is_target": False,
        },
        {
            "sender": "小明",
            "content": "好呀好呀！去哪里？",
            "timestamp": "2024-01-01 10:02",
            "is_target": True,
        },
        {
            "sender": "我",
            "content": "去公园吧",
            "timestamp": "2024-01-01 10:03",
            "is_target": False,
        },
        {
            "sender": "小明",
            "content": "嗯嗯，我最喜欢公园了",
            "timestamp": "2024-01-01 10:04",
            "is_target": True,
        },
        {
            "sender": "小明",
            "content": "等我换个衣服",
            "timestamp": "2024-01-01 10:05",
            "is_target": True,
        },
        {
            "sender": "我",
            "content": "好的不着急",
            "timestamp": "2024-01-01 10:06",
            "is_target": False,
        },
        {
            "sender": "小明",
            "content": "好啦走吧！",
            "timestamp": "2024-01-01 10:15",
            "is_target": True,
        },
    ]


@pytest.fixture
def sample_target_heavy_messages():
    """目标发言占多数的消息。"""
    msgs = []
    for i in range(10):
        msgs.append(
            {
                "sender": "小明",
                "content": f"这是ta的消息{i}",
                "timestamp": f"10:{i:02d}",
                "is_target": True,
            }
        )
    for i in range(3):
        msgs.append(
            {
                "sender": "我",
                "content": f"我的回复{i}",
                "timestamp": f"10:{10 + i:02d}",
                "is_target": False,
            }
        )
    return msgs
