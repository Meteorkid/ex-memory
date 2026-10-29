"""AstrBot 插件逻辑测试。

AstrBot 本体是个重量级运行时，为几个分支判断拉起整套环境不划算。
这里用桩替换掉 astrbot 的模块，只验证插件自身的决策逻辑：
绑定解析、群聊门禁、指令过滤、被拦截时的行为、连接回收。

注意：桩不能证明「调用的 AstrBot 接口真的存在、语义真的是这样」。
那条链路由 scripts/validation/2026-10-29-astrbot-realsend/check_send_path.py
在真实 AstrBot 上验证，改动插件时两处都要跑。
"""

import asyncio
import builtins
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "integrations" / "astrbot-plugin"
PLUGIN_MAIN = PLUGIN_DIR / "main.py"
PLUGIN_SCHEMA = PLUGIN_DIR / "_conf_schema.json"


class FakeEvent:
    """最小的 AstrMessageEvent 替身。"""

    def __init__(
        self,
        text="你好",
        session_id="s1",
        private=True,
        platform_id="test",
        sender_id="owner",
    ):
        self._text = text
        self._session_id = session_id
        self._private = private
        self._platform_id = platform_id
        self._sender_id = sender_id
        self.stopped = False
        self.replies: list[str] = []
        # 主动消息靠它定位「发到哪个平台的哪个会话」
        kind = "FriendMessage" if private else "GroupMessage"
        self.unified_msg_origin = f"{platform_id}:{kind}:{session_id}"

    def get_message_str(self):
        return self._text

    def get_session_id(self):
        return self._session_id

    def is_private_chat(self):
        return self._private

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text: str):
        self.replies.append(text)
        return text

    def stop_event(self):
        self.stopped = True


@pytest.fixture(scope="module")
def plugin_module():
    """用桩加载插件模块。"""
    if not PLUGIN_MAIN.exists():
        pytest.skip("插件源码不存在")

    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        error=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = FakeEvent

    class _MessageChain:
        """与 MessageChain().message(...) 一致：链式调用，收集文本片段。"""

        def __init__(self):
            self.parts = []

        def message(self, text):
            self.parts.append(text)
            return self

    event_mod.MessageChain = _MessageChain

    class _EventMessageType:
        ALL = "all"

    def _command(name):
        def deco(fn):
            fn._is_command = name
            return fn

        return deco

    def _event_message_type(t):
        def deco(fn):
            fn._is_message_handler = True
            return fn

        return deco

    event_mod.filter = types.SimpleNamespace(
        command=_command,
        event_message_type=_event_message_type,
        EventMessageType=_EventMessageType,
    )

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object

    class _Star:
        # 与 astrbot/core/star/base.py 的真实行为一致：保存 context。
        # 桩若漏掉这一步，save_config 相关的测试会假失败。
        def __init__(self, context, config=None):
            self.context = context

    star_mod.Star = _Star

    for name, mod in (
        ("astrbot", types.ModuleType("astrbot")),
        ("astrbot.api", api_mod),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.star", star_mod),
    ):
        sys.modules.setdefault(name, mod)

    spec = importlib.util.spec_from_file_location(
        "_exmemory_astrbot_plugin", PLUGIN_MAIN
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConfig(dict):
    """替 AstrBotConfig：真实实现是 dict 子类，并且自带 save_config。

    落盘必须走这个对象自己的 save_config。Context 上并没有这个方法——
    早先的测试把「保存」挂在 Context 上，等于替插件掩盖了一个真实缺陷。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0

    def save_config(self):
        self.save_calls += 1


class FakeContext:
    def __init__(self):
        self.sent: list[tuple[str, list[str]]] = []
        self.send_ok = True

    async def send_message(self, session, chain):
        self.sent.append((session, list(getattr(chain, "parts", []))))
        return self.send_ok


def make_plugin(plugin_module, **conf):
    ctx = FakeContext()
    plugin = plugin_module.ExMemoryPlugin(ctx)
    plugin.conf = FakeConfig(
        {
            "exmemory_dir": "/tmp/ex-memory",
            "exmemory_python": "python",
            "exmemory_token": "exm_test",
            "allow_group": False,
            "allowed_senders": {"test": ["owner"]},
            "bindings": {},
            **conf,
        }
    )
    return plugin, ctx


class ScriptedCall:
    """按工具名返回预设结果，记录调用参数。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        if isinstance(self.responses, Exception):
            raise self.responses
        if (
            tool_name == "exmemory_proactive_prepare_delivery"
            and tool_name not in self.responses
        ):
            pending = self.responses.get("exmemory_proactive_status", {}).get(
                "pending", []
            )
            tick = self.responses.get("exmemory_proactive_tick", {})
            pending = pending + [
                {"id": tick.get("message_id"), "content": tick.get("content", "")}
            ]
            for message in pending:
                if message["id"] == arguments["message_id"]:
                    return {
                        "deliverable": True,
                        "message_id": message["id"],
                        "content": message["content"],
                        "reason": "",
                    }
        return self.responses.get(tool_name, {})


async def drain(agen):
    return [item async for item in agen]


def test_unbound_session_is_ignored(plugin_module):
    plugin, _ = make_plugin(plugin_module)
    event = FakeEvent()
    plugin._call = ScriptedCall({})
    asyncio.run(drain(plugin.on_message(event)))
    assert plugin._call.calls == []
    assert event.replies == []
    assert event.stopped is False


def test_bound_session_replies_and_stops(plugin_module):
    """回复之后必须 stop_event，否则 AstrBot 的通用模型会再接一句。"""
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    event = FakeEvent()
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "我也想你"}})
    asyncio.run(drain(plugin.on_message(event)))

    assert event.replies == ["我也想你"]
    assert event.stopped is True
    assert plugin._call.calls == [
        (
            "exmemory_chat",
            {
                "slug": "awa",
                "message": "你好",
                "history": [],
                "conversation_id": plugin._default_conversation_id(event),
            },
        )
    ]


def test_default_slug_used_when_not_bound(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="fallback")
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "嗯"}})
    asyncio.run(drain(plugin.on_message(FakeEvent())))
    assert plugin._call.calls[0][1]["slug"] == "fallback"


def test_group_chat_blocked_by_default(plugin_module):
    """群聊默认关闭：否则群里任何人都能触发，且所有人的话都进上下文。"""
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "嗯"}})
    asyncio.run(drain(plugin.on_message(FakeEvent(private=False))))
    assert plugin._call.calls == []


def test_group_chat_allowed_when_enabled(plugin_module):
    plugin, _ = make_plugin(
        plugin_module, bindings=_bound(umo="test:GroupMessage:s1"), allow_group=True
    )
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "嗯"}})
    asyncio.run(drain(plugin.on_message(FakeEvent(private=False))))
    assert len(plugin._call.calls) == 1


def test_command_messages_are_not_forwarded(plugin_module):
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall({})
    asyncio.run(drain(plugin.on_message(FakeEvent(text="/help"))))
    assert plugin._call.calls == []


def test_empty_message_ignored(plugin_module):
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall({})
    asyncio.run(drain(plugin.on_message(FakeEvent(text="   "))))
    assert plugin._call.calls == []


def test_blocked_reply_surfaces_notice(plugin_module):
    """被安全闸门拦下时，必须把原因转达给用户，不能静默吞掉。"""
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall(
        {
            "exmemory_chat": {
                "blocked": True,
                "reply": "",
                "notice": {"message": "需要帮助"},
            }
        }
    )
    event = FakeEvent()
    asyncio.run(drain(plugin.on_message(event)))
    assert event.replies == ["需要帮助"]
    assert event.stopped is True


def test_connection_error_does_not_crash_plugin(plugin_module):
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall(RuntimeError("boom"))
    event = FakeEvent()
    asyncio.run(drain(plugin.on_message(event)))
    assert event.replies and "无法回应" in event.replies[0]


def test_unexpected_error_resets_session(plugin_module):
    """连接类异常要重置会话，否则会一直用坏掉的连接。"""
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin._session = object()
    plugin._call = ScriptedCall(Exception("connection closed"))
    asyncio.run(drain(plugin.on_message(FakeEvent())))
    assert plugin._session is None


def test_bind_validates_against_remote_list(plugin_module):
    """只能绑定真实存在的镜像，避免绑定一个永远调不通的 slug。"""
    plugin, ctx = make_plugin(plugin_module)
    plugin._call = ScriptedCall(
        {
            "exmemory_list_mirrors": {
                "mirrors": [{"slug": "awa", "display_name": "阿和"}]
            }
        }
    )
    event = FakeEvent(text="/exbind nope")
    asyncio.run(drain(plugin.bind(event, slug="nope")))
    assert "找不到镜像" in event.replies[0]
    assert plugin.conf.save_calls == 0


def test_bind_persists_valid_slug(plugin_module):
    plugin, ctx = make_plugin(plugin_module)
    plugin._call = ScriptedCall(
        {"exmemory_list_mirrors": {"mirrors": [{"slug": "awa"}]}}
    )
    event = FakeEvent(text="/exbind awa")
    asyncio.run(drain(plugin.bind(event, slug="awa")))
    # 绑定必须同时记住 umo：只存 slug 的话主动消息没有收件地址
    assert plugin.conf["bindings"]["test:FriendMessage:s1"] == {
        "slug": "awa",
        "umo": "test:FriendMessage:s1",
        "sender_id": "owner",
        "conversation_id": plugin._default_conversation_id(FakeEvent()),
    }
    assert plugin.conf.save_calls == 1


def test_unbind_removes_binding(plugin_module):
    plugin, ctx = make_plugin(plugin_module, bindings=_bound())
    event = FakeEvent(text="/exunbind")
    asyncio.run(drain(plugin.unbind(event)))
    assert plugin.conf["bindings"] == {}
    assert plugin.conf.save_calls == 1


def test_extract_text_concatenates_blocks(plugin_module):
    """分块返回时不能只取第一块。"""
    block_a = types.SimpleNamespace(text='{"a":')
    block_b = types.SimpleNamespace(text="1}")
    result = types.SimpleNamespace(content=[block_a, block_b])
    assert plugin_module._parse_json(plugin_module._extract_text(result)) == {"a": 1}


def test_parse_json_never_raises(plugin_module):
    assert plugin_module._parse_json("not json") == {}


def test_normalize_binding_accepts_legacy_string(plugin_module):
    assert plugin_module._normalize_binding("awa") == {
        "slug": "awa",
        "umo": "",
        "sender_id": "",
    }


# ── 主动消息 ──
#
# 这些用例守的是一条边界：插件只负责投递，任何「该不该发」的判断
# 都必须在服务端。一旦这里有分支绕过 blocked_reason，健康保护就失效了。


def _bound(slug="awa", umo="test:FriendMessage:s1", sender_id="owner"):
    return {umo: {"slug": slug, "umo": umo, "sender_id": sender_id}}


def test_proactive_delivers_pending_and_acks(plugin_module):
    plugin, ctx = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall(
        {
            "exmemory_proactive_status": {
                "config": {"enabled": True},
                "pending": [{"id": 7, "content": "今天路过那家店了"}],
                "pending_count": 1,
                "blocked_reason": None,
            },
            "exmemory_proactive_ack": {"acknowledged": 1},
        }
    )
    asyncio.run(plugin._proactive_round())

    assert ctx.sent == [("test:FriendMessage:s1", ["今天路过那家店了"])]
    assert [c[0] for c in plugin._call.calls] == [
        "exmemory_proactive_status",
        "exmemory_proactive_prepare_delivery",
        "exmemory_proactive_ack",
    ]
    assert plugin._call.calls[-1][1] == {"message_ids": [7]}


def test_proactive_ticks_only_when_queue_is_empty(plugin_module):
    """队列为空才生成。每轮都 tick 会白烧一次模型调用。"""
    plugin, ctx = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall(
        {
            "exmemory_proactive_status": {
                "config": {"enabled": True},
                "pending": [],
                "blocked_reason": None,
            },
            "exmemory_proactive_tick": {
                "sent": True,
                "message_id": 9,
                "content": "好久没说话了",
            },
            "exmemory_proactive_ack": {"acknowledged": 1},
        }
    )
    asyncio.run(plugin._proactive_round())

    assert ctx.sent == [("test:FriendMessage:s1", ["好久没说话了"])]
    assert [c[0] for c in plugin._call.calls] == [
        "exmemory_proactive_status",
        "exmemory_proactive_tick",
        "exmemory_proactive_prepare_delivery",
        "exmemory_proactive_ack",
    ]


def test_proactive_respects_health_block(plugin_module):
    """健康保护生效时一条都不发，连 tick 都不该调。"""
    plugin, ctx = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall(
        {
            "exmemory_proactive_status": {
                "config": {"enabled": True},
                "pending": [{"id": 1, "content": "最近怎么样"}],
                "blocked_reason": "guard_active",
            }
        }
    )
    asyncio.run(plugin._proactive_round())

    assert ctx.sent == []
    assert [c[0] for c in plugin._call.calls] == ["exmemory_proactive_status"]


def test_proactive_skips_when_server_switch_off(plugin_module):
    plugin, ctx = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall(
        {
            "exmemory_proactive_status": {
                "config": {"enabled": False},
                "pending": [{"id": 1, "content": "最近怎么样"}],
                "blocked_reason": None,
            }
        }
    )
    asyncio.run(plugin._proactive_round())

    assert ctx.sent == []


def test_proactive_skips_when_plugin_switch_off(plugin_module):
    plugin, ctx = make_plugin(plugin_module, bindings=_bound(), proactive_enabled=False)
    plugin._call = ScriptedCall({})
    asyncio.run(plugin._proactive_round())

    assert plugin._call.calls == []
    assert ctx.sent == []


def test_proactive_failed_send_is_not_acked(plugin_module):
    """平台没找到会话时不确认送达，否则这条消息就永久丢了。"""
    plugin, ctx = make_plugin(plugin_module, bindings=_bound())
    ctx.send_ok = False
    plugin._call = ScriptedCall(
        {
            "exmemory_proactive_status": {
                "config": {"enabled": True},
                "pending": [{"id": 5, "content": "今天降温了"}],
                "blocked_reason": None,
            }
        }
    )
    asyncio.run(plugin._proactive_round())

    assert [c[0] for c in plugin._call.calls] == [
        "exmemory_proactive_status",
        "exmemory_proactive_prepare_delivery",
    ]


def test_proactive_skips_binding_without_umo(plugin_module):
    """旧版绑定没有 umo，不能拿 session_id 硬拼一个地址去发。"""
    plugin, ctx = make_plugin(plugin_module, bindings={"s1": "awa"})
    plugin._call = ScriptedCall({})
    asyncio.run(plugin._proactive_round())

    assert plugin._call.calls == []
    assert ctx.sent == []


def test_proactive_dedupes_same_slug_across_sessions(plugin_module):
    """同一镜像绑了多个会话时只投给第一个：ack 是全局的，第二条会永远取不到。"""
    plugin, ctx = make_plugin(
        plugin_module,
        bindings={
            **_bound(),
            **_bound(umo="test:FriendMessage:s2"),
        },
    )
    plugin._call = ScriptedCall(
        {
            "exmemory_proactive_status": {
                "config": {"enabled": True},
                "pending": [{"id": 3, "content": "今天忙吗"}],
                "blocked_reason": None,
            }
        }
    )
    asyncio.run(plugin._proactive_round())

    assert ctx.sent == [("test:FriendMessage:s1", ["今天忙吗"])]


def test_legacy_binding_with_matching_umo_migrates_after_authorized_message(
    plugin_module,
):
    """只迁移来源匹配的旧绑定；无来源的旧 slug 不能自动认领。"""
    plugin, ctx = make_plugin(
        plugin_module, bindings={"s1": {"slug": "awa", "umo": "test:FriendMessage:s1"}}
    )
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "嗯"}})
    asyncio.run(drain(plugin.on_message(FakeEvent())))

    assert plugin.conf["bindings"]["test:FriendMessage:s1"] == {
        "slug": "awa",
        "umo": "test:FriendMessage:s1",
        "sender_id": "owner",
        "conversation_id": plugin._default_conversation_id(FakeEvent()),
    }
    assert plugin.conf.save_calls == 1


def test_proactive_status_command_reports_state(plugin_module):
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall(
        {
            "exmemory_proactive_status": {
                "config": {
                    "enabled": True,
                    "max_per_day": 2,
                    "quiet_start": 23,
                    "quiet_end": 8,
                },
                "pending_count": 1,
                "blocked_reason": "usage_limit_reached",
            }
        }
    )
    event = FakeEvent(text="/exproactive")
    asyncio.run(drain(plugin.proactive(event)))

    text = event.replies[0]
    assert "已开启" in text
    assert "待发送：1 条" in text
    assert "usage_limit_reached" in text


def test_proactive_toggle_sends_enabled_flag(plugin_module):
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin._call = ScriptedCall(
        {"exmemory_proactive_config": {"config": {"enabled": True}}}
    )
    event = FakeEvent(text="/exproactive on")
    asyncio.run(drain(plugin.proactive(event, action="on")))

    assert plugin._call.calls == [
        ("exmemory_proactive_config", {"slug": "awa", "enabled": True})
    ]
    assert "已开启" in event.replies[0]


def test_schema_declares_bindings_as_free_form_dict():
    """bindings 必须声明成 type=dict。

    AstrBotConfig 加载配置时会拿 schema 对账：声明成 object 的话，
    bindings 被当成固定结构（子键来自 items），插件自己写进去的会话键
    会被当成「多余配置」全部删掉。表现就是 /exbind 成功、重启后绑定消失。
    只有 dict 才是自由映射，子键会被原样保留。
    """
    schema = json.loads(PLUGIN_SCHEMA.read_text(encoding="utf-8-sig"))
    assert schema["bindings"]["type"] == "dict"


@pytest.mark.parametrize(
    "handler", ["bind", "unbind", "list_mirrors", "proactive", "on_message"]
)
@pytest.mark.parametrize("denial", ["empty", "stranger", "other_platform", "group"])
def test_every_entry_point_requires_authorized_sender(plugin_module, handler, denial):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    event = FakeEvent()
    if denial == "empty":
        plugin.conf["allowed_senders"] = {}
    elif denial == "stranger":
        event = FakeEvent(sender_id="stranger")
    elif denial == "other_platform":
        event = FakeEvent(platform_id="other")
    else:
        event = FakeEvent(private=False)
    plugin._call = ScriptedCall(
        {"exmemory_list_mirrors": {"mirrors": [{"slug": "awa"}]}}
    )
    arguments = (event, "awa") if handler == "bind" else (event,)
    asyncio.run(drain(getattr(plugin, handler)(*arguments)))
    assert plugin._call.calls == []
    assert plugin.conf["bindings"] == {}
    assert plugin.conf.save_calls == 0


def test_consecutive_messages_include_only_own_history(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "第一条回答"}})

    async def scenario():
        await drain(plugin.on_message(FakeEvent(text="第一句")))
        await drain(plugin.on_message(FakeEvent(text="接着呢")))
        await drain(plugin.on_message(FakeEvent(text="另一个会话", session_id="s2")))

    asyncio.run(scenario())
    assert plugin._call.calls[1][1]["history"] == [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "第一条回答"},
    ]
    assert plugin._call.calls[2][1]["history"] == []


def test_history_isolated_between_platform_instances(plugin_module):
    plugin, _ = make_plugin(
        plugin_module,
        default_slug="awa",
        allowed_senders={"test": ["owner"], "other": ["owner"]},
    )
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "嗯"}})

    async def scenario():
        await drain(plugin.on_message(FakeEvent(text="私密内容")))
        await drain(plugin.on_message(FakeEvent(platform_id="other")))

    asyncio.run(scenario())
    assert plugin._call.calls[1][1]["history"] == []


def test_unauthorized_proactive_binding_never_calls_mcp(plugin_module):
    umo = "test:FriendMessage:s1"
    plugin, ctx = make_plugin(
        plugin_module,
        bindings={
            umo: {"slug": "awa", "umo": umo, "sender_id": "stranger"},
        },
    )
    plugin._call = ScriptedCall({})
    asyncio.run(plugin._proactive_round())
    assert plugin._call.calls == []
    assert ctx.sent == []


def test_missing_sender_binding_cannot_proactively_send(plugin_module):
    umo = "test:FriendMessage:s1"
    plugin, ctx = make_plugin(
        plugin_module,
        bindings={
            umo: {"slug": "awa", "umo": umo},
        },
    )
    plugin._call = ScriptedCall({})
    asyncio.run(plugin._proactive_round())
    assert plugin._call.calls == []
    assert ctx.sent == []


def test_pending_delivery_requires_fresh_server_approval(plugin_module):
    umo = "test:FriendMessage:s1"
    plugin, ctx = make_plugin(
        plugin_module,
        bindings={
            umo: {"slug": "awa", "umo": umo, "sender_id": "owner"},
        },
    )
    plugin._call = ScriptedCall(
        {
            "exmemory_proactive_status": {
                "config": {"enabled": True},
                "pending": [{"id": 1, "content": "旧消息"}],
            },
            "exmemory_proactive_prepare_delivery": {
                "deliverable": False,
                "content": "",
                "reason": "quiet_hours",
                "message_id": 1,
            },
        }
    )
    asyncio.run(plugin._proactive_round())
    assert ctx.sent == []
    assert [name for name, _ in plugin._call.calls] == [
        "exmemory_proactive_status",
        "exmemory_proactive_prepare_delivery",
    ]


def test_saving_bindings_requires_config_object(plugin_module):
    """配置对象不支持保存时，不能假装保存成功。"""
    plugin, _ = make_plugin(plugin_module, bindings=_bound())
    plugin.conf = {"bindings": {}}  # 退化成普通 dict，没有 save_config

    plugin._save_bindings({"s1": {"slug": "awa", "umo": "test:private:s1"}})

    assert plugin.conf["bindings"]["s1"]["slug"] == "awa"


@pytest.mark.parametrize(
    "failure",
    [
        {"blocked": True, "notice": {"message": "暂停"}},
        RuntimeError("failed"),
        {"reply": ""},
    ],
)
def test_unsuccessful_turn_does_not_enter_history(plugin_module, failure):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall(
        failure if isinstance(failure, Exception) else {"exmemory_chat": failure}
    )

    async def scenario():
        await drain(plugin.on_message(FakeEvent(text="未成功的一轮")))
        plugin._call = ScriptedCall({"exmemory_chat": {"reply": "成功"}})
        await drain(plugin.on_message(FakeEvent(text="再次发送")))

    asyncio.run(scenario())
    assert plugin._call.calls[0][1]["history"] == []


def test_concurrent_turns_are_ordered_and_share_completed_history(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    calls = []

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def call(name, arguments):
            calls.append(arguments)
            if arguments["message"] == "第一句":
                entered.set()
                await release.wait()
            return {"reply": arguments["message"] + "回答"}

        plugin._call = call
        first = asyncio.create_task(drain(plugin.on_message(FakeEvent(text="第一句"))))
        await entered.wait()
        second = asyncio.create_task(drain(plugin.on_message(FakeEvent(text="第二句"))))
        await asyncio.sleep(0)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    assert calls[1]["history"] == [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "第一句回答"},
    ]
    assert plugin._conversation_locks == {}


def test_group_history_isolated_by_sender(plugin_module):
    plugin, _ = make_plugin(
        plugin_module,
        default_slug="awa",
        allow_group=True,
        allowed_senders={"test": ["owner", "friend"]},
    )
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "嗯"}})

    async def scenario():
        await drain(plugin.on_message(FakeEvent(text="第一人的上下文", private=False)))
        await drain(plugin.on_message(FakeEvent(private=False, sender_id="friend")))

    asyncio.run(scenario())
    assert plugin._call.calls[1][1]["history"] == []


def test_rebinding_and_unbinding_clear_conversation_history(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall(
        {
            "exmemory_chat": {"reply": "嗯"},
            "exmemory_list_mirrors": {"mirrors": [{"slug": "other"}]},
        }
    )

    async def scenario():
        await drain(plugin.on_message(FakeEvent(text="旧上下文")))
        await drain(plugin.bind(FakeEvent(), "other"))
        await drain(plugin.on_message(FakeEvent(text="新镜像")))
        await drain(plugin.unbind(FakeEvent()))
        await drain(plugin.on_message(FakeEvent(text="默认镜像")))

    asyncio.run(scenario())
    chats = [args for name, args in plugin._call.calls if name == "exmemory_chat"]
    assert [chat["slug"] for chat in chats] == ["awa", "other", "awa"]
    assert all(chat["history"] == [] for chat in chats)


def test_account_configuration_change_discards_history(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "嗯"}})

    async def scenario():
        await drain(plugin.on_message(FakeEvent(text="旧账号内容")))
        plugin.conf["exmemory_token"] = "exm_another_account"
        await drain(plugin.on_message(FakeEvent()))

    asyncio.run(scenario())
    assert plugin._call.calls[1][1]["history"] == []


def test_history_is_bounded_and_never_saved_in_config(plugin_module, monkeypatch):
    monkeypatch.setattr(plugin_module, "MAX_HISTORY_MESSAGES", 4)
    monkeypatch.setattr(plugin_module, "MAX_HISTORY_CHARACTERS", 16)
    monkeypatch.setattr(plugin_module, "MAX_HISTORY_SESSIONS", 2)
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "回答"}})

    async def scenario():
        for i in range(8):
            await drain(plugin.on_message(FakeEvent(text=f"消息{i}")))
        await drain(plugin.on_message(FakeEvent(session_id="s2")))
        await drain(plugin.on_message(FakeEvent(session_id="s3")))

    asyncio.run(scenario())
    for _name, arguments in plugin._call.calls:
        assert len(arguments["history"]) <= 4
        assert sum(len(item["content"]) for item in arguments["history"]) <= 16
    assert len(plugin._histories) == 2
    assert plugin.conf.save_calls == 0
    assert "消息" not in json.dumps(plugin.conf, ensure_ascii=False)


def test_sender_revoked_during_model_call_receives_no_reply(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")

    async def call(_name, _arguments):
        plugin.conf["allowed_senders"] = {}
        return {"reply": "私有回复"}

    plugin._call = call
    event = FakeEvent()
    asyncio.run(drain(plugin.on_message(event)))
    assert event.replies == []
    assert plugin._histories == {}


def test_sender_revoked_during_delivery_preparation_is_not_sent(plugin_module):
    plugin, ctx = make_plugin(plugin_module, bindings=_bound())
    calls = []

    async def call(name, arguments):
        calls.append(name)
        if name == "exmemory_proactive_status":
            return {
                "config": {"enabled": True},
                "pending": [{"id": 1, "content": "旧消息"}],
            }
        plugin.conf["allowed_senders"] = {}
        return {
            "deliverable": True,
            "message_id": 1,
            "content": "私有消息",
            "reason": "",
        }

    plugin._call = call
    asyncio.run(plugin._proactive_round())
    assert ctx.sent == []
    assert "exmemory_proactive_ack" not in calls


@pytest.mark.parametrize(
    "binding", ["awa", {"slug": "awa", "umo": "other:FriendMessage:s1"}]
)
def test_legacy_binding_without_matching_origin_cannot_be_claimed(
    plugin_module, binding
):
    plugin, _ = make_plugin(plugin_module, bindings={"s1": binding})
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "不应调用"}})
    asyncio.run(drain(plugin.on_message(FakeEvent())))
    assert plugin._call.calls == []
    assert plugin.conf["bindings"] == {"s1": binding}
    assert plugin.conf.save_calls == 0


def test_configuration_defaults_deny_all_senders():
    schema = json.loads(PLUGIN_SCHEMA.read_text(encoding="utf-8-sig"))
    assert schema["allowed_senders"]["type"] == "dict"
    assert schema["allowed_senders"]["default"] == {}


def test_missing_optional_mcp_dependency_does_not_leave_startup_waiting(
    plugin_module, monkeypatch
):
    original_import = builtins.__import__

    def without_mcp(name, *args, **kwargs):
        if name == "mcp":
            raise ImportError("synthetic missing optional MCP")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_mcp)
    plugin, _ = make_plugin(plugin_module)

    async def scenario():
        with pytest.raises(ImportError, match="missing optional MCP"):
            await asyncio.wait_for(plugin._ensure_session(), 1)
        assert plugin._connection_owner is None
        assert plugin._session is None

    asyncio.run(scenario())
