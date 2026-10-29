"""AstrBot 插件：让记忆体出现在聊天软件里。

这个插件刻意做得很薄——它不含任何记忆逻辑，只做三件事：

1. 把当前会话绑定到一个 ex-memory 镜像；
2. 把消息转交给 ex-memory 的 MCP Server；
3. 把回复原样发回聊天窗口。

检索、人格、安全审核、计费全部留在 ex-memory 侧。这样做的原因很实际：
一旦在这里重写一遍，两边的安全闸门就会开始漂移，
而「入口绕过了审核」这类问题恰恰是最难发现、后果最严重的一类。

也正因为它是个空壳，AstrBot 的 QQ / Telegram / 飞书 / Discord 适配层
都能直接复用，不需要为每个平台各写一遍。
"""

import asyncio
import hashlib
import json
import os
import re
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARACTERS = 12000
MAX_HISTORY_SESSIONS = 64
MCP_CONNECT_TIMEOUT_SECONDS = 30


def _normalize_binding(raw) -> dict:
    """旧绑定缺少来源或授权发送者，不能据此主动投递。"""
    if isinstance(raw, str):
        return {"slug": raw, "umo": "", "sender_id": ""}
    if isinstance(raw, dict):
        slug = raw.get("slug", "")
        umo = raw.get("umo", "")
        sender_id = raw.get("sender_id", "")
        entry = {
            "slug": slug if isinstance(slug, str) else "",
            "umo": umo if isinstance(umo, str) else "",
            "sender_id": sender_id if isinstance(sender_id, str) else "",
        }
        conversation_id = raw.get("conversation_id")
        if isinstance(conversation_id, str) and conversation_id:
            entry["conversation_id"] = conversation_id
        return entry
    return {"slug": "", "umo": "", "sender_id": ""}


def _extract_text(result) -> str:
    """从 MCP 调用结果里取出文本。

    工具返回的 content 是分块列表，直接取第 0 块会在多块时丢内容。
    """
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _parse_json(text: str) -> dict:
    """解析工具返回的 JSON。

    失败时返回空 dict 而不是抛异常——一条解析失败不该让整个插件崩掉。
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}


class ExMemoryPlugin(Star):
    """把 ex-memory 的记忆镜像接入 AstrBot 的会话。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # 不能用 config or {}：AstrBot 传进来的是 AstrBotConfig（dict 的子类），
        # 空配置时会被 or 换成普通 dict，之后就没有 save_config 了，
        # 绑定会变成「本次运行有效、重启即消失」。
        self.conf = config if config is not None else {}
        self._session = None
        self._connection_owner = None
        self._connection_stop = None
        self._lock = asyncio.Lock()
        self._proactive_task = None
        self._connected_settings = None
        self._history_settings = self._connection_settings()
        self._histories = OrderedDict()
        self._conversation_locks = {}

    def _connection_settings(self) -> tuple:
        return (
            self.conf.get("exmemory_dir", ""),
            self.conf.get("exmemory_python", "") or "python",
            self.conf.get("exmemory_token", ""),
        )

    def _refresh_history_identity(self):
        settings = self._connection_settings()
        if settings != self._history_settings:
            self._histories.clear()
            self._history_settings = settings

    def _sender_allowed(self, platform_id: str, sender_id: str) -> bool:
        allowed = self.conf.get("allowed_senders")
        if not platform_id or not sender_id or not isinstance(allowed, dict):
            return False
        senders = allowed.get(platform_id)
        return isinstance(senders, list) and sender_id in senders

    def _event_identity(self, event: AstrMessageEvent):
        platform_id = event.get_platform_id()
        sender_id = event.get_sender_id()
        umo = event.unified_msg_origin
        if not all(
            isinstance(value, str) and value for value in (platform_id, sender_id, umo)
        ):
            return None
        parts = umo.split(":", 2)
        kind = "FriendMessage" if event.is_private_chat() else "GroupMessage"
        if len(parts) != 3 or parts[:2] != [platform_id, kind] or not parts[2]:
            return None
        if not event.is_private_chat() and not self.conf.get("allow_group", False):
            return None
        if not self._sender_allowed(platform_id, sender_id):
            return None
        return umo, sender_id

    def _binding_allowed(self, umo: str, entry: dict) -> bool:
        parts = umo.split(":", 2)
        if len(parts) != 3 or not parts[2] or entry["umo"] != umo or not entry["slug"]:
            return False
        if parts[1] not in ("FriendMessage", "GroupMessage"):
            return False
        if parts[1] == "GroupMessage" and not self.conf.get("allow_group", False):
            return False
        return self._sender_allowed(parts[0], entry["sender_id"])

    @asynccontextmanager
    async def _conversation(self, umo: str):
        # 绑定切换和消息共用会话锁；计数包含等待者，避免回收后出现两把锁。
        state = self._conversation_locks.setdefault(umo, [asyncio.Lock(), 0])
        state[1] += 1
        try:
            async with state[0]:
                yield
        finally:
            state[1] -= 1
            if not state[1]:
                self._conversation_locks.pop(umo, None)

    def _clear_history(self, umo: str):
        for key in list(self._histories):
            if key[0] == umo:
                self._histories.pop(key)

    def _remember_turn(self, key: tuple, history: list, text: str, reply: str):
        history = history + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ]
        while history and (
            len(history) > MAX_HISTORY_MESSAGES
            or sum(len(item["content"]) for item in history) > MAX_HISTORY_CHARACTERS
        ):
            del history[:2]
        self._histories[key] = history
        self._histories.move_to_end(key)
        while len(self._histories) > MAX_HISTORY_SESSIONS:
            self._histories.popitem(last=False)

    async def _ensure_session(self):
        """惰性建立 MCP 连接。

        惰性而非在加载时就建连：插件加载时 ex-memory 可能还没配好，
        让用户看到「插件已加载、发消息才报错」比「插件加载失败」好排查。
        """
        async with self._lock:
            settings = self._connection_settings()
            if (
                self._session is not None
                and self._connected_settings == settings
                and self._connection_owner is not None
                and not self._connection_owner.done()
            ):
                return self._session
            await self._close_connection()
            self._refresh_history_identity()
            project_dir, _python_bin, token = settings
            if not project_dir or not token:
                raise RuntimeError(
                    "尚未完成配置。请在插件设置里填写 ex-memory 的项目路径与 MCP 令牌。"
                )

            ready = asyncio.get_running_loop().create_future()
            stop = asyncio.Event()
            self._connection_stop = stop
            self._connection_owner = asyncio.create_task(
                self._own_connection(settings, ready, stop)
            )
            try:
                session = await asyncio.shield(ready)
                if settings != self._connection_settings():
                    raise RuntimeError("连接配置已更改，请重试。")
                if self._connection_owner.done():
                    raise RuntimeError("MCP 连接已关闭，请重试。")
            except BaseException:
                await self._close_connection()
                if ready.done() and not ready.cancelled():
                    ready.exception()
                raise

            self._session = session
            self._connected_settings = settings
            logger.info("已连接到 ex-memory MCP Server")
            return self._session

    async def _own_connection(self, settings: tuple, ready, stop: asyncio.Event):
        """AnyIO 的取消作用域必须在创建它的任务内退出。"""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            project_dir, python_bin, token = settings
            env = dict(os.environ)
            env["EXMEMORY_MCP_TOKEN"] = token
            params = StdioServerParameters(
                command=python_bin,
                args=["-m", "mcp_server"],
                cwd=str(Path(project_dir).expanduser()),
                env=env,
            )
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(
                    session.initialize(), MCP_CONNECT_TIMEOUT_SECONDS
                )
                ready.set_result(session)
                await stop.wait()
        except asyncio.CancelledError:
            if not ready.done():
                ready.set_exception(RuntimeError("MCP 连接启动已取消。"))
        except Exception as e:
            if not ready.done():
                ready.set_exception(e)
            else:
                logger.warning("MCP 连接已关闭: %s", e)

    async def _call(self, tool_name: str, arguments: dict):
        """调用一个 MCP 工具并返回解析后的 dict。"""
        session = await self._ensure_session()
        settings = self._connected_settings
        result = await session.call_tool(tool_name, arguments)
        if settings != self._connection_settings():
            self._refresh_history_identity()
            raise RuntimeError("连接配置已更改，请重试。")
        if getattr(result, "isError", False):
            raise RuntimeError(_extract_text(result) or f"{tool_name} 调用失败")
        return _parse_json(_extract_text(result))

    async def _reset_session(self):
        async with self._lock:
            await self._close_connection()

    async def _close_connection(self):
        """调用者持有连接锁；只通知 owner 退出，不跨任务关闭其上下文。"""
        owner = self._connection_owner
        if self._connection_stop is not None:
            self._connection_stop.set()
        try:
            if owner is not None:
                try:
                    await asyncio.shield(owner)
                except asyncio.CancelledError:
                    await asyncio.shield(owner)
                    raise
        finally:
            self._connection_owner = None
            self._connection_stop = None
            self._session = None
            self._connected_settings = None

    async def initialize(self):
        """启动主动消息轮询。

        AstrBot 的生命周期钩子。放在这里而不是 __init__：__init__ 执行时
        事件循环未必已经跑起来，create_task 会拿到一个不属于任何 loop 的协程。
        """
        if self._proactive_task is None:
            self._proactive_task = asyncio.create_task(self._proactive_loop())

    async def terminate(self):
        """插件卸载时回收子进程，否则每次热重载都会漏一个 ex-memory 进程。"""
        if self._proactive_task is not None:
            self._proactive_task.cancel()
            try:
                await self._proactive_task
            except (asyncio.CancelledError, Exception) as e:
                # 取消时抛 CancelledError 是正常路径；其他异常也不该阻断卸载
                logger.debug("停止主动消息轮询: %s", e)
            self._proactive_task = None
        await self._reset_session()

    # 主动消息
    #
    # 判定全部在服务端：免打扰时段、每日上限、健康保护优先。
    # 这里只做「取待送 → 发出去 → 标记送达」，任何在这里加聪明的判断
    # 都是在绕过那条「保护优先于主动性」的硬约束。

    def _interval_seconds(self) -> int:
        try:
            minutes = int(self.conf.get("proactive_interval_minutes", 60))
        except (TypeError, ValueError):
            minutes = 60
        # 下限 5 分钟：判定自带日上限，但生成要调大模型，空转是有成本的
        return max(5, minutes) * 60

    def _has_deliverable_binding(self) -> bool:
        for umo, raw in self._bindings().items():
            entry = _normalize_binding(raw)
            if isinstance(umo, str) and self._binding_allowed(umo, entry):
                return True
        return False

    async def _proactive_loop(self):
        while True:
            try:
                await asyncio.sleep(self._interval_seconds())
                await self._proactive_round()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 一轮失败不能让任务死掉
                logger.error("主动消息轮询出错，稍后重试: %s", e)
                await asyncio.sleep(300)

    async def _proactive_round(self):
        if not self.conf.get("proactive_enabled", True):
            return
        # 没有可用绑定就完全不建连接：没配好的插件不该定期拉起一个子进程
        if not self._has_deliverable_binding():
            return

        # 按 slug 去重。消息在服务端按（账号, 镜像）维度排队，ack 也是全局的，
        # 同一镜像绑了多个会话时只投给第一个，否则第二条永远取不到。
        targets: dict[str, str] = {}
        for umo, raw in self._bindings().items():
            entry = _normalize_binding(raw)
            slug = entry["slug"]
            if (
                isinstance(umo, str)
                and self._binding_allowed(umo, entry)
                and slug not in targets
            ):
                targets[slug] = umo

        for slug, umo in targets.items():
            try:
                await self._deliver_one(slug, umo)
            except Exception as e:  # noqa: BLE001 — 单个镜像失败不影响其他镜像
                logger.error("主动消息投递失败 slug=%s: %s", slug, e)

    async def _deliver_one(self, slug: str, umo: str):
        settings = self._connection_settings()
        data = await self._call("exmemory_proactive_status", {"slug": slug})

        blocked = data.get("delivery_blocked_reason") or data.get("blocked_reason")
        if blocked:
            logger.debug("健康保护生效，跳过主动消息 slug=%s：%s", slug, blocked)
            return
        if not (data.get("config") or {}).get("enabled"):
            return

        pending = data.get("pending") or []
        if not pending:
            tick = await self._call("exmemory_proactive_tick", {"slug": slug})
            if not tick.get("sent"):
                return
            pending = [{"id": tick.get("message_id")}]

        delivered = []
        for message in pending:
            message_id = message.get("id")
            if message_id is None:
                continue
            prepared = await self._call(
                "exmemory_proactive_prepare_delivery",
                {"slug": slug, "message_id": message_id},
            )
            if prepared.get("deliverable") is not True:
                continue
            content = prepared.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            entry = _normalize_binding(self._bindings().get(umo))
            if (
                settings != self._connection_settings()
                or not self.conf.get("proactive_enabled", True)
                or entry["slug"] != slug
                or not self._binding_allowed(umo, entry)
            ):
                return
            try:
                ok = await self.context.send_message(
                    umo, MessageChain().message(content.strip())
                )
            except Exception as e:  # noqa: BLE001 — 一条失败不该中断整批
                logger.error("发送主动消息失败 slug=%s: %s", slug, e)
                continue
            if ok:
                delivered.append(message_id)
            else:
                # 平台没找到会话就不确认送达，下一轮会重试
                logger.warning("平台未找到目标会话，主动消息留待重试 slug=%s", slug)

        if delivered:
            # 配置换了账号后不能把旧队列 ID 交给新账号确认。
            if settings == self._connection_settings():
                await self._call("exmemory_proactive_ack", {"message_ids": delivered})
            logger.info("已投递 %d 条主动消息 slug=%s", len(delivered), slug)

    # 会话绑定

    def _bindings(self) -> dict:
        bindings = self.conf.get("bindings") or {}
        return dict(bindings) if isinstance(bindings, dict) else {}

    def _save_bindings(self, bindings: dict):
        self.conf["bindings"] = bindings
        # 落盘要用插件配置对象自己的 save_config。
        # Context 上没有这个方法（它只管运行时），调错了会被下面的 except 吞掉，
        # 表现就是 /exbind 当场成功、重启后绑定没了。
        save = getattr(self.conf, "save_config", None)
        if not callable(save):
            logger.warning(
                "当前配置对象不支持保存，绑定只在本次运行内有效：%s",
                type(self.conf).__name__,
            )
            return
        try:
            save()
        except Exception as e:
            logger.warning("保存会话绑定失败: %s", e)

    def _resolve_slug(self, event: AstrMessageEvent) -> str:
        if self._event_identity(event) is None:
            return ""
        self._ensure_umo(event)
        entry = _normalize_binding(self._bindings().get(event.unified_msg_origin))
        if entry["umo"] == event.unified_msg_origin and entry["sender_id"]:
            return entry["slug"]
        return self.conf.get("default_slug", "")

    def _default_conversation_id(self, event: AstrMessageEvent) -> str:
        # 平台/会话/发送者共同确定来源；账号和镜像由服务端另行隔离。
        identity = json.dumps(
            [event.unified_msg_origin, event.get_sender_id()],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return "im_" + hashlib.sha256(identity.encode()).hexdigest()

    def _resolve_conversation_id(self, event: AstrMessageEvent) -> str:
        default = self._default_conversation_id(event)
        if not event.is_private_chat():
            return default
        entry = _normalize_binding(self._bindings().get(event.unified_msg_origin))
        if (
            entry["umo"] == event.unified_msg_origin
            and entry["sender_id"] == event.get_sender_id()
        ):
            selected = entry.get("conversation_id", "")
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", selected):
                return selected
        return default

    def _memory_context(self, event: AstrMessageEvent, *, private_only=False):
        identity = self._event_identity(event)
        if identity is None or (private_only and not event.is_private_chat()):
            return None
        slug = self._resolve_slug(event)
        if not slug:
            return None
        return (
            identity,
            self._connection_settings(),
            slug,
            self._resolve_conversation_id(event),
        )

    def _memory_context_current(self, event, context, *, private_only=False):
        return self._memory_context(event, private_only=private_only) == context

    @staticmethod
    def _latest_answer(data):
        for message in reversed(data.get("messages") or []):
            if (
                isinstance(message, dict)
                and message.get("role") == "assistant"
                and isinstance(message.get("id"), str)
                and message["id"]
            ):
                return message
        return None

    def _ensure_umo(self, event: AstrMessageEvent):
        """只迁移来源完整且与可信事件精确匹配的旧绑定。"""
        identity = self._event_identity(event)
        if identity is None:
            return
        umo, sender_id = identity
        bindings = self._bindings()
        session_id = event.get_session_id()
        key = umo if umo in bindings else session_id
        if key not in bindings:
            return
        entry = _normalize_binding(bindings[key])
        if not entry["slug"] or entry["umo"] != umo:
            return
        if key == umo and entry["sender_id"]:
            return
        bindings.pop(key)
        bindings[umo] = {
            "slug": entry["slug"],
            "umo": umo,
            "sender_id": sender_id,
            "conversation_id": self._default_conversation_id(event),
        }
        self._save_bindings(bindings)

    @filter.command("exbind")
    async def bind(self, event: AstrMessageEvent, slug: str = ""):
        """用法: /exbind {镜像名} — 把当前会话绑定到某个记忆镜像。"""
        identity = self._event_identity(event)
        if identity is None:
            event.stop_event()
            yield event.plain_result("当前发送者或会话未获授权，请联系机器人管理员。")
            return
        if not slug:
            yield event.plain_result(
                "用法：/exbind {镜像名}。可以用 /exlist 查看有哪些镜像。"
            )
            return

        umo, sender_id = identity
        async with self._conversation(umo):
            if self._event_identity(event) != identity:
                return
            try:
                data = await self._call("exmemory_list_mirrors", {})
            except Exception as e:
                yield event.plain_result(f"无法连接 ex-memory：{e}")
                return
            if self._event_identity(event) != identity:
                return
            names = [m["slug"] for m in data.get("mirrors", [])]
            if slug not in names:
                hint = (
                    f"当前可用：{', '.join(names)}"
                    if names
                    else "该账号下还没有任何镜像。"
                )
                yield event.plain_result(f"找不到镜像 [{slug}]。{hint}")
                return

            bindings = self._bindings()
            bindings[umo] = {
                "slug": slug,
                "umo": umo,
                "sender_id": sender_id,
                "conversation_id": self._default_conversation_id(event),
            }
            legacy = _normalize_binding(bindings.get(event.get_session_id()))
            if legacy["umo"] == umo:
                bindings.pop(event.get_session_id(), None)
            self._clear_history(umo)
            self._save_bindings(bindings)
            yield event.plain_result(
                f"已把当前会话绑定到 [{slug}]，现在直接发消息即可对话。"
            )

    @filter.command("exunbind")
    async def unbind(self, event: AstrMessageEvent):
        """用法: /exunbind — 解除当前会话的绑定。"""
        identity = self._event_identity(event)
        if identity is None:
            event.stop_event()
            yield event.plain_result("当前发送者或会话未获授权，请联系机器人管理员。")
            return
        umo, _sender_id = identity
        async with self._conversation(umo):
            if self._event_identity(event) != identity:
                return
            self._ensure_umo(event)
            self._clear_history(umo)
            bindings = self._bindings()
            if bindings.pop(umo, None):
                self._save_bindings(bindings)
                yield event.plain_result("已解除绑定。")
            else:
                yield event.plain_result("当前会话没有绑定任何镜像。")

    @filter.command("exlist")
    async def list_mirrors(self, event: AstrMessageEvent):
        """用法: /exlist — 列出可用的记忆镜像。"""
        identity = self._event_identity(event)
        if identity is None:
            event.stop_event()
            yield event.plain_result("当前发送者或会话未获授权，请联系机器人管理员。")
            return
        try:
            data = await self._call("exmemory_list_mirrors", {})
        except Exception as e:
            yield event.plain_result(f"无法连接 ex-memory：{e}")
            return

        if self._event_identity(event) != identity:
            return

        mirrors = data.get("mirrors", [])
        if not mirrors:
            yield event.plain_result("该账号下还没有任何镜像。")
            return

        current = self._resolve_slug(event)
        lines = []
        for m in mirrors:
            mark = " <- 当前" if m["slug"] == current else ""
            lines.append(f"- {m['slug']}（{m.get('display_name') or '未命名'}）{mark}")
        yield event.plain_result("可用镜像：\n" + "\n".join(lines))

    @filter.command("exproactive")
    async def proactive(self, event: AstrMessageEvent, action: str = ""):
        """用法: /exproactive [on|off] — 查看或开关 ta 主动发消息。"""
        identity = self._event_identity(event)
        if identity is None:
            event.stop_event()
            yield event.plain_result("当前发送者或会话未获授权，请联系机器人管理员。")
            return
        slug = self._resolve_slug(event)
        if not slug:
            yield event.plain_result("请先用 /exbind 绑定一个记忆镜像。")
            return

        action = (action or "").strip().lower()
        if action not in ("", "status", "on", "off"):
            yield event.plain_result("用法：/exproactive [on|off]")
            return

        try:
            if action in ("on", "off"):
                data = await self._call(
                    "exmemory_proactive_config",
                    {"slug": slug, "enabled": action == "on"},
                )
                if self._event_identity(event) != identity:
                    return
                config = data.get("config") or {}
                state = "已开启" if config.get("enabled") else "已关闭"
                yield event.plain_result(f"{state} [{slug}] 的主动消息。")
                return

            data = await self._call("exmemory_proactive_status", {"slug": slug})
        except RuntimeError as e:
            yield event.plain_result(f"无法连接 ex-memory：{e}")
            return
        except Exception as e:  # noqa: BLE001 — 连接中断时给可读提示
            await self._reset_session()
            yield event.plain_result(f"查询主动消息状态失败：{e}")
            return

        if self._event_identity(event) != identity:
            return

        config = data.get("config") or {}
        lines = [
            f"镜像：{slug}",
            f"主动消息：{'已开启' if config.get('enabled') else '已关闭'}"
            "（默认关闭，用 /exproactive on 打开）",
            f"每日上限：{config.get('max_per_day')} 条",
            f"免打扰：{config.get('quiet_start')}:00 – {config.get('quiet_end')}:00",
            f"待发送：{data.get('pending_count', 0)} 条",
        ]
        if data.get("blocked_reason"):
            lines.append(f"当前被拦下：{data['blocked_reason']}")
        if not self.conf.get("proactive_enabled", True):
            lines.append("注意：插件的主动消息轮询已关闭，开了服务端开关也不会推送。")
        yield event.plain_result("\n".join(lines))

    @filter.command("exsessions")
    async def sessions(self, event: AstrMessageEvent):
        """用法: /exsessions — 私聊中列出这个镜像可继续的持久会话。"""
        context = self._memory_context(event, private_only=True)
        if context is None:
            event.stop_event()
            yield event.plain_result(
                "请在获授权的私聊中绑定镜像后使用；群聊不能列出其他会话。"
            )
            return
        event.stop_event()
        _identity, _settings, slug, current = context
        try:
            data = await self._call("exmemory_conversations", {"slug": slug})
        except Exception:
            if self._memory_context_current(event, context, private_only=True):
                yield event.plain_result("读取会话失败，请稍后重试。")
            return
        if not self._memory_context_current(event, context, private_only=True):
            return
        rows = data.get("conversations") or []
        lines = []
        for row in rows[:20]:
            cid = row.get("conversation_id", "")
            if not isinstance(cid, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", cid
            ):
                continue
            label = str(row.get("label") or "未命名会话")[:80]
            lines.append(f"- {label}：{cid}" + ("（当前）" if cid == current else ""))
        yield event.plain_result(
            "可继续的会话：\n" + "\n".join(lines) + "\n用 /exsession 会话ID 明确选择。"
            if lines
            else "还没有已保存的会话，先发一条消息即可创建。"
        )

    @filter.command("exsession")
    async def select_session(self, event: AstrMessageEvent, conversation_id: str = ""):
        """用法: /exsession {会话ID} — 私聊中明确继续一个已保存会话。"""
        context = self._memory_context(event, private_only=True)
        if context is None:
            event.stop_event()
            yield event.plain_result(
                "请在获授权的私聊中绑定镜像后使用；群聊始终保持独立会话。"
            )
            return
        event.stop_event()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", conversation_id):
            yield event.plain_result(
                "用法：/exsession 会话ID。先用 /exsessions 查看可选会话。"
            )
            return
        (umo, sender_id), _settings, slug, _current = context
        async with self._conversation(umo):
            if not self._memory_context_current(event, context, private_only=True):
                return
            try:
                data = await self._call("exmemory_conversations", {"slug": slug})
            except Exception:
                if self._memory_context_current(event, context, private_only=True):
                    yield event.plain_result("读取会话失败，请稍后重试。")
                return
            if not self._memory_context_current(event, context, private_only=True):
                return
            if conversation_id not in {
                row.get("conversation_id") for row in data.get("conversations") or []
            }:
                yield event.plain_result(
                    "该镜像没有这个已保存会话，请先用 /exsessions 查看。"
                )
                return
            bindings = self._bindings()
            bindings[umo] = {
                "slug": slug,
                "umo": umo,
                "sender_id": sender_id,
                "conversation_id": conversation_id,
            }
            self._clear_history(umo)
            self._save_bindings(bindings)
            yield event.plain_result("已切换会话，接下来会继续该会话的已保存历史。")

    @filter.command("excorrect")
    async def correct(self, event: AstrMessageEvent, content: str = ""):
        """用法: /excorrect {纠正内容} — 明确纠正当前会话最新的已保存回答。"""
        context = self._memory_context(event)
        if context is None:
            event.stop_event()
            yield event.plain_result("当前发送者或会话未获授权，请先绑定镜像。")
            return
        event.stop_event()
        # 指令解析器可能按空格拆参数，纠正正文应保留完整尾部。
        raw = event.get_message_str() or ""
        command = re.match(r"^/?excorrect(?:\s+|$)(.*)", raw.strip(), re.DOTALL)
        if command:
            content = command.group(1)
        content = content.strip()
        if not content or len(content) > 500:
            yield event.plain_result("用法：/excorrect 纠正内容，限1至500字。")
            return
        (umo, _sender_id), _settings, slug, conversation_id = context
        async with self._conversation(umo):
            if not self._memory_context_current(event, context):
                return
            try:
                data = await self._call(
                    "exmemory_conversation_messages",
                    {"slug": slug, "conversation_id": conversation_id},
                )
                if not self._memory_context_current(event, context):
                    return
                answer = self._latest_answer(data)
                if answer is None:
                    yield event.plain_result("当前会话还没有已保存的回答可纠正。")
                    return
                result = await self._call(
                    "exmemory_correct",
                    {
                        "slug": slug,
                        "content": content,
                        "conversation_id": conversation_id,
                        "message_id": answer["id"],
                        "source_ids": [],
                    },
                )
            except Exception:
                if self._memory_context_current(event, context):
                    yield event.plain_result("纠正未能保存，请稍后重试。")
                return
            if not self._memory_context_current(event, context):
                return
            if result.get("ok") is True:
                yield event.plain_result("已记录纠正，后续回答会读取更新后的记忆。")
            else:
                yield event.plain_result("服务端未确认保存，请重试。")

    @filter.command("exsources")
    async def sources(self, event: AstrMessageEvent):
        """用法: /exsources — 查看当前会话最新回答的检索参考。"""
        context = self._memory_context(event)
        if context is None:
            event.stop_event()
            yield event.plain_result("当前发送者或会话未获授权，请先绑定镜像。")
            return
        event.stop_event()
        _identity, _settings, slug, conversation_id = context
        try:
            data = await self._call(
                "exmemory_conversation_messages",
                {"slug": slug, "conversation_id": conversation_id},
            )
        except Exception:
            if self._memory_context_current(event, context):
                yield event.plain_result("读取参考失败，请稍后重试。")
            return
        if not self._memory_context_current(event, context):
            return
        answer = self._latest_answer(data)
        references = answer.get("sources") if answer else []
        if not references:
            yield event.plain_result("这条回答没有已保存的检索参考。")
            return
        kinds = {
            "imported_chat": "原始聊天",
            "narrative": "口述材料",
            "generated_summary": "生成摘要",
            "conversation_history": "历史人机对话",
            "unknown": "来源未分类",
        }
        lines = ["本次检索参考（不等于逐句事实验证）："]
        for reference in references[:4]:
            kind = kinds.get(reference.get("kind"), "来源未分类")
            source = str(reference.get("source") or "")[:60]
            text = str(reference.get("text") or "").strip()[:400]
            if text:
                lines.append(f"[{kind}{' · ' + source if source else ''}] {text}")
        yield event.plain_result("\n\n".join(lines))

    # 消息转发

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """把可信会话的消息和有界上下文转给记忆体。"""
        identity = self._event_identity(event)
        if identity is None:
            return
        text = (event.get_message_str() or "").strip()
        if not text or text.startswith("/"):
            return

        umo, sender_id = identity
        async with self._conversation(umo):
            if self._event_identity(event) != identity:
                return
            slug = self._resolve_slug(event)
            if not slug:
                return
            event.stop_event()
            self._refresh_history_identity()
            settings = self._connection_settings()
            conversation_id = self._resolve_conversation_id(event)
            key = (umo, sender_id, slug, conversation_id)
            history = [dict(item) for item in self._histories.get(key, [])]
            try:
                if self.conf.get("search_before_reply", False):
                    try:
                        await self._call(
                            "exmemory_search_memory",
                            {"slug": slug, "query": text, "top_k": 3},
                        )
                    except Exception as e:
                        logger.debug("检索原话失败（不阻断对话）: %s", e)
                if (
                    self._event_identity(event) != identity
                    or settings != self._connection_settings()
                    or slug != self._resolve_slug(event)
                    or conversation_id != self._resolve_conversation_id(event)
                ):
                    return
                data = await self._call(
                    "exmemory_chat",
                    {
                        "slug": slug,
                        "message": text,
                        "history": history,
                        "conversation_id": conversation_id,
                    },
                )
            except RuntimeError as e:
                logger.error("ex-memory 调用失败: %s", e)
                yield event.plain_result(f"记忆体暂时无法回应：{e}")
                return
            except Exception as e:
                logger.error("ex-memory 连接异常，将重建连接: %s", e)
                await self._reset_session()
                yield event.plain_result("记忆体连接中断，请再试一次。")
                return

            if (
                self._event_identity(event) != identity
                or settings != self._connection_settings()
                or slug != self._resolve_slug(event)
                or conversation_id != self._resolve_conversation_id(event)
            ):
                return
            if data.get("blocked"):
                notice = data.get("notice") or {}
                yield event.plain_result(
                    notice.get("message") or "这条消息没有被回应。"
                )
                return
            reply = (data.get("reply") or "").strip()
            if not reply:
                return
            self._remember_turn(key, history, text, reply)
            yield event.plain_result(reply)
