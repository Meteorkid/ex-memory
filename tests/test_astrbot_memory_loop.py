"""IM 入口的持久会话、显式纠正和来源交互。"""

import asyncio

import pytest

from tests.test_astrbot_plugin import (
    FakeEvent,
    ScriptedCall,
    drain,
    make_plugin,
    plugin_module as plugin_module,
)


def call_chat(plugin, event):
    asyncio.run(drain(plugin.on_message(event)))
    return next(
        args for name, args in reversed(plugin._call.calls) if name == "exmemory_chat"
    )


def test_default_conversation_survives_restart_and_separates_identities(plugin_module):
    first, _ = make_plugin(
        plugin_module,
        default_slug="awa",
        allow_group=True,
        allowed_senders={"test": ["owner", "second"], "other": ["owner"]},
    )
    first._call = ScriptedCall({"exmemory_chat": {"reply": "回答"}})
    original = call_chat(first, FakeEvent())["conversation_id"]
    assert original.startswith("im_")
    assert len(original) <= 128
    restarted, _ = make_plugin(plugin_module, default_slug="awa")
    restarted._call = ScriptedCall({"exmemory_chat": {"reply": "回答"}})
    assert call_chat(restarted, FakeEvent())["conversation_id"] == original
    other_ids = [
        call_chat(first, event)["conversation_id"]
        for event in (
            FakeEvent(platform_id="other"),
            FakeEvent(sender_id="second"),
            FakeEvent(private=False),
            FakeEvent(session_id="s2"),
        )
    ]
    assert len(set([original, *other_ids])) == 5


@pytest.mark.parametrize(
    "handler", ["sessions", "select_session", "correct", "sources"]
)
@pytest.mark.parametrize("denial", ["empty", "sender", "platform", "group"])
def test_new_commands_require_authorized_sender(plugin_module, handler, denial):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    event = FakeEvent()
    if denial == "empty":
        plugin.conf["allowed_senders"] = {}
    elif denial == "sender":
        event = FakeEvent(sender_id="stranger")
    elif denial == "platform":
        event = FakeEvent(platform_id="other")
    else:
        event = FakeEvent(private=False)
    plugin._call = ScriptedCall({})
    asyncio.run(drain(getattr(plugin, handler)(event)))
    assert plugin._call.calls == []
    assert plugin.conf["bindings"] == {}


@pytest.mark.parametrize("handler", ["sessions", "select_session"])
def test_enabled_group_cannot_list_or_select_other_conversations(
    plugin_module, handler
):
    plugin, _ = make_plugin(plugin_module, default_slug="awa", allow_group=True)
    plugin._call = ScriptedCall({})
    arguments = (
        (FakeEvent(private=False), "web")
        if handler == "select_session"
        else (FakeEvent(private=False),)
    )
    asyncio.run(drain(getattr(plugin, handler)(*arguments)))
    assert plugin._call.calls == []


def test_private_session_selection_is_persisted_and_used_after_restart(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall(
        {
            "exmemory_conversations": {
                "conversations": [{"conversation_id": "web", "label": "网页对话"}]
            }
        }
    )
    event = FakeEvent()
    asyncio.run(drain(plugin.select_session(event, "web")))
    binding = plugin.conf["bindings"][event.unified_msg_origin]
    assert binding["conversation_id"] == "web"
    assert plugin.conf.save_calls == 1
    restarted, _ = make_plugin(plugin_module, bindings=plugin.conf["bindings"])
    restarted._call = ScriptedCall({"exmemory_chat": {"reply": "接着聊"}})
    assert call_chat(restarted, event)["conversation_id"] == "web"


def test_unknown_selection_does_not_change_binding(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall(
        {"exmemory_conversations": {"conversations": [{"conversation_id": "web"}]}}
    )
    asyncio.run(drain(plugin.select_session(FakeEvent(), "other")))
    assert plugin.conf["bindings"] == {}


def test_group_ignores_manually_saved_private_conversation_id(plugin_module):
    event = FakeEvent(private=False)
    plugin, _ = make_plugin(
        plugin_module,
        allow_group=True,
        bindings={
            event.unified_msg_origin: {
                "slug": "awa",
                "umo": event.unified_msg_origin,
                "sender_id": "owner",
                "conversation_id": "web",
            }
        },
    )
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "回答"}})
    assert call_chat(plugin, event)["conversation_id"] != "web"


def test_selected_conversation_is_not_inherited_by_another_sender(plugin_module):
    event = FakeEvent()
    plugin, _ = make_plugin(
        plugin_module,
        allowed_senders={"test": ["owner", "second"]},
        bindings={
            event.unified_msg_origin: {
                "slug": "awa",
                "umo": event.unified_msg_origin,
                "sender_id": "owner",
                "conversation_id": "web",
            }
        },
    )
    plugin._call = ScriptedCall({"exmemory_chat": {"reply": "回答"}})
    assert call_chat(plugin, FakeEvent(sender_id="second"))["conversation_id"] != "web"


def test_correction_uses_latest_saved_answer_without_cached_state(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall(
        {
            "exmemory_conversation_messages": {
                "messages": [
                    {"id": "old", "role": "assistant", "content": "旧"},
                    {
                        "id": "latest",
                        "role": "assistant",
                        "content": "新",
                        "sources": [{"id": "ref"}],
                    },
                ]
            },
            "exmemory_correct": {"ok": True, "revision": "new"},
        }
    )
    event = FakeEvent()
    asyncio.run(drain(plugin.correct(event, "ta不会叫我宝贝")))
    read_name, read_args = plugin._call.calls[0]
    write_name, write_args = plugin._call.calls[1]
    assert read_name == "exmemory_conversation_messages"
    assert write_name == "exmemory_correct"
    assert write_args == {
        "slug": "awa",
        "content": "ta不会叫我宝贝",
        "conversation_id": read_args["conversation_id"],
        "message_id": "latest",
        "source_ids": [],
    }
    assert any("已记录" in text for text in event.replies)


def test_correction_without_saved_answer_does_not_write(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall(
        {
            "exmemory_conversation_messages": {
                "messages": [{"role": "user", "content": "未回答"}]
            }
        }
    )
    asyncio.run(drain(plugin.correct(FakeEvent(), "ta不会这样说")))
    assert [name for name, _ in plugin._call.calls] == [
        "exmemory_conversation_messages"
    ]


def test_correction_preserves_spaces_after_command(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall(
        {
            "exmemory_conversation_messages": {
                "messages": [{"id": "last", "role": "assistant", "content": "回答"}]
            },
            "exmemory_correct": {"ok": True},
        }
    )
    event = FakeEvent(text="/excorrect ta 不会 say that")
    asyncio.run(drain(plugin.correct(event, "ta")))
    assert plugin._call.calls[-1][1]["content"] == "ta 不会 say that"


def test_sources_distinguish_original_and_generated_and_are_bounded(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    plugin._call = ScriptedCall(
        {
            "exmemory_conversation_messages": {
                "messages": [
                    {
                        "id": "last",
                        "role": "assistant",
                        "content": "回答",
                        "sources": [
                            {
                                "id": "1",
                                "kind": "imported_chat",
                                "source": "wechat",
                                "text": "原话",
                            },
                            {
                                "id": "2",
                                "kind": "generated_summary",
                                "text": "摘要" + "x" * 10000,
                            },
                        ],
                    }
                ]
            }
        }
    )
    event = FakeEvent()
    asyncio.run(drain(plugin.sources(event)))
    output = "\n".join(event.replies)
    assert "原始聊天" in output and "生成摘要" in output
    assert len(output) < 2500


@pytest.mark.parametrize(
    "handler", ["sessions", "select_session", "correct", "sources"]
)
def test_revocation_while_reading_suppresses_reply_and_writes(plugin_module, handler):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    calls = []

    async def call(name, arguments):
        calls.append((name, arguments))
        plugin.conf["allowed_senders"] = {}
        return {
            "conversations": [{"conversation_id": "web", "label": "秘密"}],
            "messages": [
                {
                    "role": "assistant",
                    "id": "last",
                    "content": "秘密",
                    "sources": [{"text": "秘密"}],
                }
            ],
        }

    plugin._call = call
    event = FakeEvent()
    args = (
        (event, "web")
        if handler == "select_session"
        else (event, "纠正")
        if handler == "correct"
        else (event,)
    )
    asyncio.run(drain(getattr(plugin, handler)(*args)))
    assert event.replies == []
    assert len(calls) == 1
    assert plugin.conf["bindings"] == {}


@pytest.mark.parametrize(
    "handler", ["sessions", "select_session", "correct", "sources"]
)
def test_account_switch_while_reading_does_not_disclose_old_account_data(
    plugin_module, handler
):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    calls = []

    async def call(name, arguments):
        calls.append(name)
        plugin.conf["exmemory_token"] = "synthetic-new-account"
        return {
            "conversations": [{"conversation_id": "web"}],
            "messages": [
                {
                    "id": "last",
                    "role": "assistant",
                    "content": "旧账号",
                    "sources": [{"text": "旧账号"}],
                }
            ],
        }

    plugin._call = call
    event = FakeEvent()
    args = (
        (event, "web")
        if handler == "select_session"
        else (event, "纠正")
        if handler == "correct"
        else (event,)
    )
    asyncio.run(drain(getattr(plugin, handler)(*args)))
    assert event.replies == []
    assert len(calls) == 1
    assert plugin.conf["bindings"] == {}


def test_changed_selected_conversation_suppresses_in_flight_reply(plugin_module):
    plugin, _ = make_plugin(plugin_module, default_slug="awa")
    event = FakeEvent()

    async def call(name, arguments):
        plugin.conf["bindings"] = {
            event.unified_msg_origin: {
                "slug": "awa",
                "umo": event.unified_msg_origin,
                "sender_id": "owner",
                "conversation_id": "web",
            }
        }
        return {"reply": "来自旧会话"}

    plugin._call = call
    asyncio.run(drain(plugin.on_message(event)))
    assert event.replies == []
    assert plugin._histories == {}
