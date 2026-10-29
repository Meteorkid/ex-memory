#!/usr/bin/env python3
"""用真实的 AstrBot 代码验证主动消息的投递链路。

`tests/test_astrbot_plugin.py` 用 types.ModuleType 把整个 astrbot 换成了桩，
只验证插件自己的分支判断。它有一个盲区：凡是「插件调用的 AstrBot 接口
本身不存在或语义不同」这类问题，桩永远发现不了。

这个脚本换一种做法：装上真的 AstrBot 4.29.3，用它真的类跑一遍。

真实参与的部分
    - Context.send_message 的 umo 解析与平台实例匹配（含找不到时的 False 分支）
    - MessageSession.from_str 对 `platform_id:message_type:session_id` 的拆分
    - Telegram 适配器的 send_by_session → send_with_client（含 `#话题` 拆分）
    - 飞书适配器的 send_by_session（含私聊 open_id / 群聊 chat_id 的选择）
    - AstrBotConfig 的读写，即插件配置到底能不能落盘
    - 插件本体 main.py，原样加载，不做任何替换

被替换的部分（都在网络边界之外，且都不影响「发到哪个会话」这一判断）
    - Telegram SDK 的 send_message / send_chat_action：换成记录器
    - 飞书 SDK 的 send_message_chain：换成记录器
    - 插件对 MCP 工具的一次调用：脚本里给定返回值（MCP 侧另有测试覆盖）

用法
    ~/.venvs/astrbot312/bin/python \\
        scripts/validation/2026-10-29-astrbot-realsend/check_send_path.py

退出码 0 表示全部通过，1 表示有失败项。
"""

# ruff: noqa: E402 —— 指标上报开关必须在 import astrbot 之前设好

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

# AstrBot 每次 send_by_session 都会往数据库写一条平台统计，这里没有数据库，
# 关掉它可以避免无关的报错淹没真正的结果。
os.environ.setdefault("ASTRBOT_DISABLE_METRICS", "1")

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_MAIN = ROOT / "integrations" / "astrbot-plugin" / "main.py"
PLUGIN_SCHEMA = ROOT / "integrations" / "astrbot-plugin" / "_conf_schema.json"

# ---- 真实 AstrBot 代码 ----
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.manager import PlatformManager
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.lark.lark_adapter import LarkPlatformAdapter
from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
from astrbot.core.platform.sources.telegram.tg_adapter import TelegramPlatformAdapter
from astrbot.core.star import Context

TG_ID = "tg1"
LARK_ID = "lark1"

# 明确不可用的占位值，拼出来是为了不写成一段看起来像真凭证的字符串
TG_PLACEHOLDER_CRED = "0" * 9 + ":" + "offline-check"
LARK_PLACEHOLDER_APPID = "cli_" + "offlinecheck"
LARK_PLACEHOLDER_SECRET = "offline-check"

# 记录器累积的「平台侧真实收到的内容」
SENT: list[dict] = []


class _DefaultConf:
    """替 astrbot_config_mgr 做一次默认配置回退。

    Context.get_config(umo=...) 会转到 AstrBotConfigManager.get_conf，
    而后者要 UmopConfigRouter + SharedPreferences + 数据库才能构造。
    它只决定「这次会话用哪份配置」，不参与消息投递，所以这里直接返回默认配置——
    这也正是 AstrBotConfigManager 在会话没有专属配置时的真实行为。
    """

    def __init__(self, conf: AstrBotConfig):
        self._conf = conf

    def get_conf(self, umo=None):  # noqa: ANN001, ANN201
        return self._conf


class Env:
    """一套跑得起来的 AstrBot 最小装配。"""

    def __init__(self, tmpdir: Path):
        self.tmpdir = tmpdir
        self.queue: asyncio.Queue = asyncio.Queue()

        self.core_config = AstrBotConfig(
            config_path=str(tmpdir / "cmd_config.json"),
            default_config={"platform": [], "platform_settings": {}},
        )
        self.platform_manager = PlatformManager(self.core_config, self.queue)

        # Context 的其余依赖（数据库、人格、知识库等）与 send_message 无关，
        # 这里少调了几个不参与判断的组件，但 send_message 本身是原样的类方法。
        self.context = Context.__new__(Context)
        self.context.platform_manager = self.platform_manager
        self.context._config = self.core_config
        self.context.astrbot_config_mgr = _DefaultConf(self.core_config)
        self.context.message_history_manager = None

        self.tg = TelegramPlatformAdapter(
            {"id": TG_ID, "telegram_token": TG_PLACEHOLDER_CRED}, {}, self.queue
        )
        self.lark = LarkPlatformAdapter(
            {
                "id": LARK_ID,
                "app_id": LARK_PLACEHOLDER_APPID,
                "app_secret": LARK_PLACEHOLDER_SECRET,
            },
            {},
            self.queue,
        )
        self.platform_manager.platform_insts.extend([self.tg, self.lark])

        self._lark_original = LarkMessageEvent.send_message_chain
        self._install_network_recorders()

    # -- 把网络调用换成记录器 ------------------------------------------------

    def _install_network_recorders(self):
        SENT.clear()

        # 记录器一律接 *args：挂到类上时会被绑成方法，第一个位置参数是 self
        async def _record_tg(*args, **kwargs):
            SENT.append(
                {
                    "platform": TG_ID,
                    "text": kwargs.get("text"),
                    "chat_id": kwargs.get("chat_id"),
                    "message_thread_id": kwargs.get("message_thread_id"),
                    "parse_mode": kwargs.get("parse_mode"),
                }
            )

        async def _record_action(*args, **kwargs):
            return None

        # python-telegram-bot 的 Bot 实例不让赋值，只能替换类方法
        bot_cls = type(self.tg.client)
        self._tg_cls = bot_cls
        self._tg_original_send = bot_cls.send_message
        self._tg_original_action = bot_cls.send_chat_action
        bot_cls.send_message = _record_tg
        bot_cls.send_chat_action = _record_action

        async def _record_lark(
            message_chain,
            lark_client,
            reply_message_id=None,
            receive_id=None,
            receive_id_type=None,
            fallback_chat_id=None,
        ):
            SENT.append(
                {
                    "platform": LARK_ID,
                    "text": _chain_text(message_chain),
                    "receive_id": receive_id,
                    "receive_id_type": receive_id_type,
                    "fallback_chat_id": fallback_chat_id,
                }
            )

        LarkMessageEvent.send_message_chain = staticmethod(_record_lark)

    def restore(self):
        LarkMessageEvent.send_message_chain = self._lark_original
        self._tg_cls.send_message = self._tg_original_send
        self._tg_cls.send_chat_action = self._tg_original_action

    # -- 事件构造 ------------------------------------------------------------

    def make_event(
        self,
        platform_id: str,
        session_id: str,
        text: str,
        message_type: MessageType = MessageType.FRIEND_MESSAGE,
    ):
        """用真实 Platform.create_event 造一个事件（真实 AstrMessageEvent）。"""
        platform = self.tg if platform_id == TG_ID else self.lark
        message = AstrBotMessage()
        message.type = message_type
        message.session_id = session_id
        message.message_str = text
        message.message_id = "m1"
        message.self_id = "bot1"
        message.sender = MessageMember(user_id="u1", nickname="tester")
        message.message = [Plain(text)]
        message.raw_message = None
        if message_type == MessageType.GROUP_MESSAGE:
            message.group_id = session_id
        return platform.create_event(message)

    def make_plugin_config(self, path: Path) -> AstrBotConfig:
        """按 star_manager 的做法，用 _conf_schema.json 生成插件配置。"""
        with open(PLUGIN_SCHEMA, encoding="utf-8-sig") as f:
            schema = json.load(f)
        config = AstrBotConfig(config_path=str(path), schema=schema)
        config["allowed_senders"] = {TG_ID: ["u1"], LARK_ID: ["u1"], "ghost": ["u1"]}
        return config


def _chain_text(chain: MessageChain) -> str:
    return "".join(c.text for c in chain.chain if isinstance(c, Plain))


def load_plugin_module():
    """原样加载插件 main.py（不替换 astrbot）。"""
    spec = importlib.util.spec_from_file_location(
        "exmemory_plugin_under_test", PLUGIN_MAIN
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = ""):
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not ok:
        line += f"\n       {detail}"
    print(line)


async def check_routing(env: Env):
    """A. umo → 平台实例 → 平台侧会话，是否真能对上。"""
    SENT.clear()
    await env.context.send_message(
        f"{TG_ID}:FriendMessage:12345", MessageChain().message("在吗")
    )
    got = SENT[-1] if SENT else {}
    record(
        "A1 Telegram 私聊：umo 里的 session_id 就是最终 chat_id",
        got.get("chat_id") == "12345" and got.get("text") == "在吗",
        f"平台侧收到 {got}",
    )

    SENT.clear()
    await env.context.send_message(
        f"{TG_ID}:GroupMessage:-100123#77", MessageChain().message("群里的话题")
    )
    got = SENT[-1] if SENT else {}
    record(
        "A2 Telegram 超级群话题：session_id 里的 # 被拆成 chat_id + message_thread_id",
        got.get("chat_id") == "-100123" and got.get("message_thread_id") == "77",
        f"平台侧收到 {got}",
    )

    SENT.clear()
    await env.context.send_message(
        f"{LARK_ID}:FriendMessage:ou_abc", MessageChain().message("飞书私聊")
    )
    got = SENT[-1] if SENT else {}
    record(
        "A3 飞书私聊：按 open_id 投递",
        got.get("receive_id_type") == "open_id" and got.get("receive_id") == "ou_abc",
        f"平台侧收到 {got}",
    )

    # 飞书群聊的真实 session_id 就是 chat_id（见 lark_adapter 里 abm.session_id = group_id）
    SENT.clear()
    await env.context.send_message(
        f"{LARK_ID}:GroupMessage:oc_group", MessageChain().message("飞书群聊")
    )
    got = SENT[-1] if SENT else {}
    record(
        "A4 飞书群聊：按 chat_id 投递",
        got.get("receive_id_type") == "chat_id" and got.get("receive_id") == "oc_group",
        f"平台侧收到 {got}",
    )

    # 历史版本会把 chat_id 写成 chat_id%chat_id，适配器仍会剥掉前缀
    SENT.clear()
    await env.context.send_message(
        f"{LARK_ID}:GroupMessage:oc_old%oc_old",
        MessageChain().message("飞书群聊（历史格式）"),
    )
    got = SENT[-1] if SENT else {}
    record(
        "A5 飞书群聊（历史 % 格式）：前缀被剥掉，仍按 chat_id 投递",
        got.get("receive_id_type") == "chat_id" and got.get("receive_id") == "oc_old",
        f"平台侧收到 {got}",
    )

    # umo 里的平台 id 不存在 → Context.send_message 自己返回 False
    sent_before = len(SENT)
    ok = await env.context.send_message(
        "ghost:FriendMessage:1", MessageChain().message("没人收")
    )
    record(
        "A6 平台 id 不存在：返回 False，且没有任何内容被发出",
        ok is False and len(SENT) == sent_before,
        f"返回值={ok!r}，新增发送 {len(SENT) - sent_before} 条",
    )

    # 缺冒号的 umo → MessageSession.from_str 直接拒绝
    try:
        await env.context.send_message("not-a-umo", MessageChain().message("x"))
        record("A7 非法 umo：抛 ValueError", False, "没有抛异常，可能被静默吞掉")
    except ValueError as exc:
        record("A7 非法 umo：抛 ValueError", True, str(exc))
    except Exception as exc:  # noqa: BLE001
        record(
            "A7 非法 umo：抛 ValueError", False, f"抛的是 {type(exc).__name__}: {exc}"
        )


async def check_delivery_failure(env: Env, plugin_module):
    """B. 平台侧投递失败时，send_message 的返回值靠不靠得住。"""

    # 让真实的飞书发送函数抛异常，模拟「应用被移出会话」
    async def _boom(*args, **kwargs):
        raise RuntimeError("[Lark] A proactive message cannot be delivered")

    LarkMessageEvent.send_message_chain = staticmethod(_boom)
    raised = False
    returned = None
    try:
        returned = await env.context.send_message(
            f"{LARK_ID}:FriendMessage:ou_gone", MessageChain().message("发不出去")
        )
    except Exception:  # noqa: BLE001
        raised = True
    finally:
        LarkMessageEvent.send_message_chain = staticmethod(env._lark_original)
    record(
        "B1 平台侧投递失败时异常会传出来，而不是被换成 True",
        raised,
        f"没有抛异常，返回了 {returned!r}——这意味着失败会被当成投递成功",
    )

    # 插件在异常路径上必须「不 ack」，否则这条消息就永久丢了
    config = env.make_plugin_config(env.tmpdir / "plugin_conf_ack.json")
    config["bindings"] = {
        f"{LARK_ID}:FriendMessage:ou_gone": {
            "slug": "demo",
            "umo": f"{LARK_ID}:FriendMessage:ou_gone",
            "sender_id": "u1",
        }
    }
    plugin = plugin_module.ExMemoryPlugin(env.context, config)

    calls: list[tuple[str, dict]] = []

    async def fake_call(tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        if tool_name == "exmemory_proactive_status":
            return {
                "config": {"enabled": True},
                "pending": [{"id": 7, "content": "该不该发"}],
            }
        if tool_name == "exmemory_proactive_prepare_delivery":
            return {
                "deliverable": True,
                "message_id": arguments["message_id"],
                "content": "该不该发",
                "reason": "",
            }
        return {}

    plugin._call = fake_call  # type: ignore[method-assign]

    LarkMessageEvent.send_message_chain = staticmethod(_boom)
    try:
        await plugin._proactive_round()
    finally:
        LarkMessageEvent.send_message_chain = staticmethod(env._lark_original)

    acked = [c for c in calls if c[0] == "exmemory_proactive_ack"]
    record(
        "B2 投递抛异常时不 ack，消息留给下一轮重试",
        not acked and any(c[0] == "exmemory_proactive_prepare_delivery" for c in calls),
        f"调用链 {calls}，ack={acked}",
    )

    # 平台找不到（返回 False）同样不能 ack
    calls.clear()
    ghost_config = env.make_plugin_config(env.tmpdir / "plugin_conf_ghost.json")
    ghost_config["bindings"] = {
        "ghost:FriendMessage:ou_ghost": {
            "slug": "demo",
            "umo": "ghost:FriendMessage:ou_ghost",
            "sender_id": "u1",
        }
    }
    plugin2 = plugin_module.ExMemoryPlugin(env.context, ghost_config)
    plugin2._call = fake_call  # type: ignore[method-assign]
    await plugin2._proactive_round()
    acked = [c for c in calls if c[0] == "exmemory_proactive_ack"]
    record(
        "B3 平台找不到（send_message 返回 False）时不 ack",
        not acked and any(c[0] == "exmemory_proactive_prepare_delivery" for c in calls),
        f"调用链 {calls}，ack={acked}",
    )


async def check_binding_persistence(env: Env, plugin_module):
    """C. /exbind 之后，绑定到底有没有落到配置文件里。"""
    conf_path = env.tmpdir / "plugin_conf_bind.json"
    if conf_path.exists():
        conf_path.unlink()
    config = env.make_plugin_config(conf_path)
    plugin = plugin_module.ExMemoryPlugin(env.context, config)

    async def fake_call(tool_name, arguments):
        if tool_name == "exmemory_list_mirrors":
            return {"mirrors": [{"slug": "demo", "display_name": "示例"}]}
        return {}

    plugin._call = fake_call  # type: ignore[method-assign]

    event = env.make_event(TG_ID, "12345", "/exbind demo")
    async for _ in plugin.bind(event, "demo"):
        pass

    # 直接看磁盘：进程重启后 AstrBot 只会从这里读回来
    on_disk = {}
    if conf_path.exists():
        # AstrBot 写配置时带 BOM，用 utf-8-sig 读
        on_disk = json.loads(conf_path.read_text(encoding="utf-8-sig"))
    bindings = on_disk.get("bindings") or {}
    record(
        "C1 /exbind 之后绑定写进了插件配置文件",
        bool(bindings),
        f"配置文件 {conf_path.name} 里没有 bindings，实际键={list(on_disk)}",
    )

    # 用同一份配置新建一个插件实例，等价于 AstrBot 重启
    fresh = env.make_plugin_config(conf_path)
    reborn = plugin_module.ExMemoryPlugin(env.context, fresh)
    resolved = reborn._resolve_slug(env.make_event(TG_ID, "12345", "普通消息"))
    record(
        "C2 重启后（新建实例读同一份配置）绑定仍然生效",
        resolved == "demo",
        f"重启后解析到的镜像是 {resolved!r}",
    )


async def check_proactive_end_to_end(env: Env, plugin_module):
    """D. 一轮主动消息：取待送 → 真实适配器投递 → 只 ack 成功的那些。"""
    conf_path = env.tmpdir / "plugin_conf_proactive.json"
    if conf_path.exists():
        conf_path.unlink()
    config = env.make_plugin_config(conf_path)
    config["bindings"] = {
        f"{TG_ID}:FriendMessage:12345": {
            "slug": "demo",
            "umo": f"{TG_ID}:FriendMessage:12345",
            "sender_id": "u1",
        }
    }
    plugin = plugin_module.ExMemoryPlugin(env.context, config)

    calls: list[tuple[str, dict]] = []

    async def fake_call(tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        if tool_name == "exmemory_proactive_status":
            return {
                "config": {"enabled": True},
                "pending": [
                    {"id": 1, "content": "今天降温了"},
                    {"id": 2, "content": "记得带外套"},
                ],
            }
        if tool_name == "exmemory_proactive_prepare_delivery":
            return {
                "deliverable": True,
                "message_id": arguments["message_id"],
                "content": {1: "今天降温了", 2: "记得带外套"}[arguments["message_id"]],
                "reason": "",
            }
        return {"ok": True}

    plugin._call = fake_call  # type: ignore[method-assign]

    SENT.clear()
    await plugin._proactive_round()

    texts = [s.get("text") for s in SENT if s.get("platform") == TG_ID]
    record(
        "D1 两条待送消息都通过真实 Telegram 适配器发出去了",
        texts == ["今天降温了", "记得带外套"],
        f"平台侧实际收到 {texts}",
    )

    ack = [c for c in calls if c[0] == "exmemory_proactive_ack"]
    record(
        "D2 ack 的 id 与真正发出去的完全一致",
        bool(ack) and ack[0][1].get("message_ids") == [1, 2],
        f"ack 内容 {ack}",
    )

    # 第二条失败：第一条该 ack，第二条绝对不能 ack
    conf_path2 = env.tmpdir / "plugin_conf_partial.json"
    if conf_path2.exists():
        conf_path2.unlink()
    config2 = env.make_plugin_config(conf_path2)
    config2["bindings"] = {
        f"{TG_ID}:FriendMessage:12345": {
            "slug": "demo",
            "umo": f"{TG_ID}:FriendMessage:12345",
            "sender_id": "u1",
        }
    }
    plugin2 = plugin_module.ExMemoryPlugin(env.context, config2)
    calls.clear()
    plugin2._call = fake_call  # type: ignore[method-assign]

    state = {"n": 0}
    healthy = env._tg_cls.send_message

    async def flaky(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("平台侧限流")
        return await healthy(*args, **kwargs)

    SENT.clear()
    env._tg_cls.send_message = flaky
    try:
        await plugin2._proactive_round()
    finally:
        env._tg_cls.send_message = healthy

    ack = [c for c in calls if c[0] == "exmemory_proactive_ack"]
    record(
        "D3 一条成功一条失败：只 ack 成功的那条",
        bool(ack) and ack[0][1].get("message_ids") == [1],
        f"ack 内容 {ack}（期望只含 1）",
    )


async def check_sender_and_history(env: Env, plugin_module):
    config = env.make_plugin_config(env.tmpdir / "plugin_conf_auth.json")
    config["default_slug"] = "demo"
    plugin = plugin_module.ExMemoryPlugin(env.context, config)
    calls = []

    async def fake_call(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "exmemory_chat":
            return {"blocked": False, "reply": "回复"}
        return {"mirrors": []}

    plugin._call = fake_call
    event = env.make_event(TG_ID, "12345", "第一句")
    record(
        "E1 真实事件提供精确平台实例与发送者",
        event.get_platform_id() == TG_ID and event.get_sender_id() == "u1",
    )
    async for _ in plugin.on_message(event):
        pass
    async for _ in plugin.on_message(env.make_event(TG_ID, "12345", "第二句")):
        pass
    chats = [args for name, args in calls if name == "exmemory_chat"]
    record(
        "E2 第二轮携带本会话历史",
        len(chats) == 2
        and chats[1].get("history")
        == [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "回复"},
        ],
    )
    calls.clear()
    config["allowed_senders"] = {}
    async for _ in plugin.list_mirrors(env.make_event(TG_ID, "12345", "/exlist")):
        pass
    record("E3 撤销发送者授权后命令不访问MCP", calls == [])


async def main() -> int:
    if not PLUGIN_MAIN.exists():
        print(f"找不到插件源码：{PLUGIN_MAIN}")
        return 1

    plugin_module = load_plugin_module()
    with tempfile.TemporaryDirectory(prefix="astrbot-realsend-") as tmp:
        env = Env(Path(tmp))
        try:
            await check_routing(env)
            await check_delivery_failure(env, plugin_module)
            await check_binding_persistence(env, plugin_module)
            await check_proactive_end_to_end(env, plugin_module)
            await check_sender_and_history(env, plugin_module)
        finally:
            env.restore()

    failed = [r for r in RESULTS if not r[1]]
    print()
    print(
        f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
