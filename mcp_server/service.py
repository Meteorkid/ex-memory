"""MCP 工具的业务实现。

与 MCP 传输层完全解耦：这里只吃普通 Python 参数、返回普通 dict，
不出现任何 FastMCP 类型。这样单测可以直接调用，不必起传输层。

最重要的约束：不能绕过任何一道安全闸门。
/api/chat 的顺序是 危机检测 → 强度保护 → 输入审核 → 引擎 → 输出审核 → 落库 → 结算。
MCP 只是另一个入口，换入口不代表换规则。顺序也必须一致——
危机检测必须排在内容审核之前，否则被误判为「违规」的求救信息会被挡回去。
"""

import logging
import threading
from typing import Optional

logger = logging.getLogger("ex-memory")


class ServiceError(Exception):
    """面向客户端的业务错误。message 会直接展示给 MCP 客户端。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_engine_lock = threading.Lock()
_engine_cache: dict = {}
_ENGINE_CACHE_MAX = 32


def _get_engine(slug: str, user_id: int):
    """按 (user_id, slug) 缓存引擎。

    键必须含 user_id——slug 是全局命名空间，只按 slug 缓存会在镜像
    删除重建后把前任 owner 的人格内容泄漏给新用户。
    """
    from core.memory_state import get_memory_signature
    from server.model_settings import get_settings_revision

    key = (
        user_id,
        slug,
        get_settings_revision(user_id),
        get_memory_signature(slug, user_id),
    )
    with _engine_lock:
        engine = _engine_cache.get(key)
    if engine is not None:
        return engine

    from core.factory import create_engine_and_store

    engine, _store, _embedder = create_engine_and_store(slug, owner=user_id)

    with _engine_lock:
        existing = _engine_cache.get(key)
        if existing is not None:
            return existing
        if len(_engine_cache) >= _ENGINE_CACHE_MAX:
            del _engine_cache[next(iter(_engine_cache))]
        _engine_cache[key] = engine
    return engine


def invalidate_engine(slug: Optional[str] = None, user_id: Optional[int] = None):
    """清空引擎缓存。模型设置或人格更新后调用，避免继续用旧客户端。"""
    with _engine_lock:
        if slug is None and user_id is None:
            _engine_cache.clear()
            return
        for key in list(_engine_cache):
            if (slug is None or key[1] == slug) and (
                user_id is None or key[0] == user_id
            ):
                del _engine_cache[key]


def _resolve_slug(slug: str, user_id: int) -> str:
    """校验 slug 归属，把异常映射为 ServiceError。

    刻意保留 404 与 403 的区别：403 意味着「存在但不是你的」，
    不该让调用方误以为镜像不存在。
    """
    from core.validation import validate_slug
    from core.exe_access import assert_exe_access

    try:
        slug = validate_slug(slug)
        assert_exe_access(slug, user_id)
    except ValueError as e:
        raise ServiceError("invalid_slug", str(e)) from None
    except FileNotFoundError as e:
        raise ServiceError("not_found", str(e)) from None
    except PermissionError as e:
        raise ServiceError("forbidden", str(e)) from None
    return slug


def _check_ready(slug: str, user_id: int) -> None:
    """镜像是否已完成构建（导入型镜像的流水线可能还没跑完）。"""
    from core.mirror_store import mirror_store

    store = mirror_store(slug, user_id)
    if store.exists("meta.json"):
        meta = store.read_json("meta.json")
        if (
            meta.get("creation_source") == "chat_import"
            and meta.get("pipeline_state") != "completed"
        ):
            raise ServiceError("not_ready", "镜像仍在构建中，请等待导入流水线完成")


def _notice_result(notice: dict) -> dict:
    """被闸门拦下时的统一返回。

    reply 为空串并带 blocked 标记，让客户端能区分「没说话」和「说了空话」。
    """
    return {"reply": "", "blocked": True, "notice": notice}


def search_memory(
    user_id: int,
    slug: str,
    query: str,
    top_k: int = 5,
    target_only: bool = True,
) -> dict:
    """检索 ta 的原话。

    target_only 默认开启：只召回 dominant_speaker == 'target' 的片段。
    评测表明这个过滤有代价——8 轮窗口下候选池枯竭，Recall@5 掉到 21.1%。
    但对「还原 ta 的语气」这个用途，混入自己的话比召回率下降更糟。

    返回 score 而不只是文本：调用方需要能判断这次召回可不可信，
    只给文本等于逼着客户端盲信。
    """
    slug = _resolve_slug(slug, user_id)
    query = (query or "").strip()
    if not query:
        raise ServiceError("empty_query", "检索内容不能为空")
    top_k = max(1, min(int(top_k), 50))

    from config import get_embedding_config
    from core.factory import build_vector_store
    from memory.embedder import Embedder

    try:
        emb_cfg = get_embedding_config(user_id)
    except Exception as e:
        raise ServiceError("no_embedding", f"未配置 Embedding 模型：{e}") from None

    if not emb_cfg.get("api_key"):
        raise ServiceError("no_embedding", "未配置 Embedding 模型，无法检索记忆")

    embedder = Embedder(
        api_key=emb_cfg["api_key"],
        base_url=emb_cfg["base_url"],
        model=emb_cfg["model"],
        owner=user_id,
        model_config=emb_cfg,
    )
    store = build_vector_store(slug, user_id)

    if store.count() == 0:
        return {"query": query, "count": 0, "results": [], "note": "该镜像没有向量记忆"}

    if target_only:
        hits = store.search_target_only(query, embedder, top_k=top_k)
    else:
        hits = store.search(query, embedder, top_k=top_k)

    results = []
    for hit in hits:
        meta = hit.get("metadata") or {}
        results.append(
            {
                "text": hit.get("display_text", ""),
                "score": round(float(hit.get("score", 0)), 4),
                "speaker": meta.get("dominant_speaker", ""),
                "source": meta.get("source", ""),
            }
        )
    return {"query": query, "count": len(results), "results": results}


def chat(
    user_id: int,
    slug: str,
    message: str,
    history: Optional[list[dict]] = None,
    conversation_id: Optional[str] = None,
) -> dict:
    slug = _resolve_slug(slug, user_id)
    if conversation_id is None:
        return _chat_unlocked(user_id, slug, message, history)
    from core.conversation_store import (
        conversation_request_lock,
        validate_conversation_id,
    )

    try:
        conversation_id = validate_conversation_id(conversation_id)
        with conversation_request_lock(slug, user_id, conversation_id):
            return _chat_unlocked(user_id, slug, message, history, conversation_id)
    except TimeoutError:
        raise ServiceError(
            "conversation_busy", "这个会话正在回复，请稍后再发"
        ) from None
    except ValueError as error:
        raise ServiceError("invalid_conversation", str(error)) from None


def _chat_unlocked(
    user_id: int,
    slug: str,
    message: str,
    history: Optional[list[dict]] = None,
    conversation_id: Optional[str] = None,
) -> dict:
    """单轮对话，完整走 /api/chat 的安全闸门与计费。"""
    slug = _resolve_slug(slug, user_id)

    from core.validation import validate_user_input
    from core.memory_loop import request_history, chat_with_sources
    from server.safety_gate import check_crisis, check_input, check_output
    from server.usage_guard import check_limit
    from server.billing_gate import QuotaBlocked, release, reserve, settle

    try:
        message = validate_user_input(message) if message else ""
    except ValueError as e:
        raise ServiceError("invalid_message", str(e)) from None
    if not message:
        raise ServiceError("empty_message", "消息不能为空")

    history = request_history(slug, user_id, history, conversation_id)

    # 顺序不可调整，理由见文件头：危机检测必须早于内容审核，
    # 强度保护必须晚于危机检测（达到时长上限的人仍要拿得到危机响应）。
    notice = check_crisis(user_id, slug, message)
    if notice is not None:
        return _notice_result(notice)

    limit_notice = check_limit(user_id)
    if limit_notice is not None:
        return _notice_result(limit_notice)

    blocked = check_input(user_id, slug, message)
    if blocked is not None:
        return _notice_result(blocked)

    _check_ready(slug, user_id)
    engine = _get_engine(slug, user_id)

    # 配额预扣排在审核之后：被闸门拦下的请求根本没调 LLM，不该扣用户额度
    try:
        account_id, _outcome = reserve(user_id)
    except QuotaBlocked as e:
        return _notice_result(e.notice)

    try:
        reply, stickers, usage, sources = chat_with_sources(
            engine, message, history, conversation_id
        )

        blocked_output = check_output(user_id, slug, reply)
        if blocked_output is not None:
            release(account_id)
            return _notice_result(blocked_output)

        from core.persona_style import split_reply

        reply = "\n".join(split_reply(reply, getattr(engine, "style_profile", None)))
        turn_id = None
        try:
            from core.conversation_store import append_turn

            turn_id = append_turn(
                slug,
                user_id,
                message,
                reply,
                stickers=stickers,
                source="mcp",
                conversation_id=conversation_id or "web",
                sources=sources,
            )
        except (OSError, ValueError) as e:
            # 持久化失败不该让这一轮对话蒸发，但必须留痕——
            # 否则搜索与统计会静默缺数据，排查时无从下手
            logger.warning("MCP 对话持久化失败 slug=%s: %s", slug, e)

        settle(
            account_id,
            usage,
            user_id=user_id,
            slug=slug,
            provider=getattr(engine, "last_provider", ""),
            model=getattr(engine, "model", ""),
        )

        # blocked 显式给出 false：成功与拦截用同一个字段表达，客户端
        # 只需判断 result["blocked"]，不必再靠「有没有这个键」去猜
        result = {
            "reply": reply,
            "slug": slug,
            "stickers": list(stickers or []),
            "blocked": False,
            "conversation_id": conversation_id or "web",
            "message_id": f"{turn_id}-assistant" if turn_id else None,
            "sources": sources if turn_id else [],
        }
        if turn_id and conversation_id is not None:
            try:
                from core.memory_loop import schedule_archive

                schedule_archive(slug, user_id, conversation_id)
            except Exception as error:
                logger.warning("会话归档调度失败 slug=%s: %s", slug, error)
        if usage:
            result["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                "completion_tokens": getattr(usage, "completion_tokens", 0),
            }
        return result
    except Exception as e:
        release(account_id)
        logger.error("MCP 对话失败: %s", e, exc_info=True)
        raise ServiceError("chat_failed", "对话失败，请稍后重试") from None


def correct(
    user_id: int,
    slug: str,
    content: str,
    conversation_id: str = "web",
    message_id: Optional[str] = None,
    source_ids: Optional[list[str]] = None,
) -> dict:
    slug = _resolve_slug(slug, user_id)
    if not isinstance(content, str) or not content.strip():
        raise ServiceError("empty_correction", "纠正内容不能为空")
    if len(content) > 500:
        raise ServiceError("too_long", "纠正内容请控制在 500 字以内")
    from core.memory_loop import save_correction

    try:
        result = save_correction(
            slug, user_id, content, conversation_id, message_id, source_ids
        )
    except ValueError as error:
        raise ServiceError("invalid_correction", str(error)) from None
    invalidate_engine(slug, user_id)
    return result


def conversations(user_id: int, slug: str) -> dict:
    slug = _resolve_slug(slug, user_id)
    from core.memory_loop import conversation_list

    return conversation_list(slug, user_id)


def conversation_messages(user_id: int, slug: str, conversation_id: str) -> dict:
    slug = _resolve_slug(slug, user_id)
    from core.memory_loop import conversation_messages as read_messages

    try:
        return read_messages(slug, user_id, conversation_id)
    except ValueError as error:
        raise ServiceError("invalid_conversation", str(error)) from None


def list_corrections(user_id: int, slug: str) -> dict:
    """列出已有纠正，便于客户端判断某件事是否已经纠正过。"""
    slug = _resolve_slug(slug, user_id)
    from core.corrections import load

    entries = load(slug, owner=user_id)
    return {
        "count": len(entries),
        "corrections": [
            {
                "content": e.get("content", ""),
                "count": e.get("count", 1),
                "created_at": e.get("created_at", ""),
            }
            for e in entries
        ],
    }


def reflect(user_id: int, slug: str) -> dict:
    """启动关系反思分析。异步执行，返回 task_id。

    刻意做成异步：反思要跑 7 个维度的 LLM 分析，同步返回会让 MCP 客户端
    卡在一个没有进度的调用上，多数客户端会直接超时。
    """
    slug = _resolve_slug(slug, user_id)

    from core.tasks import enqueue
    from server.task_handlers import TASK_REFLECT

    try:
        task_id = enqueue(
            TASK_REFLECT, user_id, {"slug": slug, "owner": user_id}, slug=slug
        )
    except ValueError as e:
        raise ServiceError("task_unavailable", str(e)) from None
    return {"task_id": task_id, "status": "queued", "slug": slug}


def status(user_id: int, slug: str, include_corrections: bool = False) -> dict:
    """镜像的记忆状态快照：情绪记忆、关系状态、对话规模。

    只读，不触发任何 LLM 调用——MCP 客户端可能频繁查询，
    这里一旦调模型，成本会不可控。
    """
    slug = _resolve_slug(slug, user_id)

    from core.conversation_store import load_jsonl_messages
    from core.emotional_memory import load_emotional_memories
    from core.mirror_store import mirror_store
    from core.relationship import get_state

    memories = load_emotional_memories(slug, owner=user_id)
    state = get_state(slug, owner=user_id)
    messages = load_jsonl_messages(slug, user_id)

    store = mirror_store(slug, user_id)
    meta = store.read_json("meta.json") if store.exists("meta.json") else {}

    # 情绪记忆是按类别分桶的字典，不是列表——
    # 数条数要累加各桶，不能对字典取 len
    if isinstance(memories, dict):
        memory_count = sum(
            len(v) for v in memories.values() if isinstance(v, (list, tuple))
        )
        memory_preview = dict(memories)
    else:
        memory_count = len(memories)
        memory_preview = list(memories)[:20]

    result = {
        "slug": slug,
        "display_name": meta.get("display_name", meta.get("name", slug)),
        "message_count": len(messages),
        "emotional_memory_count": memory_count,
        "emotional_memories": memory_preview,
        "relationship_state": state,
        "created_at": meta.get("created_at", ""),
    }
    if include_corrections:
        from core.corrections import load as load_corrections

        entries = load_corrections(slug, owner=user_id)
        result["corrections"] = [
            {"content": e.get("content", ""), "count": e.get("count", 1)}
            for e in entries
        ]
    return result


def list_mirrors(user_id: int) -> dict:
    """列出当前账号可见的镜像。

    MCP 客户端没有网页可以浏览，必须能仅通过协议发现有哪些镜像可用，
    否则每次对话都得让用户先去别处查 slug。
    """
    from pathlib import Path

    from core.exe_access import iter_accessible_exes
    from core.mirror_store import mirror_store

    mirrors = []
    for entry in iter_accessible_exes(user_id):
        # iter_accessible_exes 产出的是目录 Path，不是 slug 字符串
        slug = entry.name if isinstance(entry, Path) else entry
        try:
            store = mirror_store(slug, user_id)
            meta = store.read_json("meta.json") if store.exists("meta.json") else {}
        except Exception as e:
            logger.debug("读取镜像 meta 失败 slug=%s: %s", slug, e)
            meta = {}
        mirrors.append(
            {
                "slug": slug,
                "display_name": meta.get("display_name", meta.get("name", slug)),
            }
        )
    return {"count": len(mirrors), "mirrors": mirrors}


# ── 主动消息（FR-071 / FR-072）──
#
# 判定规则全部留在 core/proactive.py：免打扰时段、每日上限、健康保护优先。
# 这一层只做归属校验、异常翻译，以及「取待送 / 标记送达」的协议适配。
#
# 刻意不在这里重算任何一条规则。两套判定一旦漂移，插件绕过的正是
# 「健康保护优先于主动性」这条硬约束——而这个产品的用户群本就有沉溺风险。

_NO_TRIGGER_REASON = (
    "当前不满足触发条件：可能未开启主动消息、处于免打扰时段、"
    "已达当日上限，或不在触发时段（8–10 点 / 21–23 点 / 沉默超过 48 小时）"
)


def _blocked_reason(user_id: int) -> Optional[str]:
    """健康保护是否禁止推送。查不到状态时按禁止处理。

    与 core.proactive.health_blocks 的保守策略保持一致：宁可少推一条，
    也不要在无法确认用户状态时主动打扰。
    """
    from core.proactive import health_blocks

    try:
        return health_blocks(user_id)
    except Exception as e:  # noqa: BLE001 — 查不到就是不能确认，按禁止处理
        logger.warning("查询健康状态失败 user=%s: %s", user_id, e)
        return "无法确认使用状态"


def _delivery_blocked_reason(user_id: int, slug: str) -> Optional[str]:
    from core.proactive import delivery_blocked_reason

    try:
        return delivery_blocked_reason(slug, user_id, owner=user_id)
    except Exception as e:  # noqa: BLE001 — 无法确认时不能投递
        logger.warning("查询投递限制失败 user=%s slug=%s: %s", user_id, slug, e)
        return "无法确认主动消息投递状态"


def proactive_status(user_id: int, slug: str) -> dict:
    """主动消息的配置、待送队列与当前阻断原因。

    只读，不调用大模型——客户端可以放心高频轮询。
    blocked_reason 非空时，即便触发条件满足也不会推送。
    """
    slug = _resolve_slug(slug, user_id)

    from core.proactive import get_config, pending_messages

    config = get_config(slug, owner=user_id)
    pending = pending_messages(user_id, slug)
    return {
        "slug": slug,
        "config": config,
        "pending": pending,
        "pending_count": len(pending),
        "blocked_reason": _blocked_reason(user_id),
        "delivery_blocked_reason": _delivery_blocked_reason(user_id, slug),
    }


def proactive_prepare_delivery(user_id: int, slug: str, message_id: int) -> dict:
    """每条消息发送前校验归属、当前投递限制和输出；不生成、不收费。"""
    slug = _resolve_slug(slug, user_id)
    if type(message_id) is not int or message_id <= 0:
        raise ServiceError("invalid_argument", "message_id 必须是正整数")

    from core.proactive import dismiss_message, pending_messages
    from server.safety_gate import check_output

    result = {
        "deliverable": False,
        "message_id": message_id,
        "content": "",
        "reason": "",
    }
    reason = _delivery_blocked_reason(user_id, slug)
    if reason:
        result["reason"] = reason
        return result

    message = next(
        (m for m in pending_messages(user_id, slug) if m["id"] == message_id), None
    )
    if message is None:
        result["reason"] = "消息不存在或已不在待送队列中"
        return result
    content = (message.get("content") or "").strip()
    if not content:
        dismiss_message(message_id, user_id, slug)
        result["reason"] = "待送消息为空"
        return result

    # 历史队列可能来自旧版本或其他生成入口，也必须在下发前审核。
    try:
        blocked = check_output(user_id, slug, content)
    except Exception as e:  # noqa: BLE001 — 审核失败不能返回待投递原文
        logger.warning("待送消息审核失败 user=%s slug=%s: %s", user_id, slug, e)
        raise ServiceError(
            "review_unavailable", "暂时无法审核待送消息，请稍后重试"
        ) from None
    if blocked is not None:
        dismiss_message(message_id, user_id, slug)
        result["reason"] = blocked.get("message") or "待送消息未通过内容审核"
        return result
    reason = _delivery_blocked_reason(user_id, slug)
    if reason:
        result["reason"] = reason
        return result

    result.update(deliverable=True, content=content)
    return result


def proactive_config(
    user_id: int,
    slug: str,
    enabled: Optional[bool] = None,
    max_per_day: Optional[int] = None,
    quiet_start: Optional[int] = None,
    quiet_end: Optional[int] = None,
) -> dict:
    """修改主动消息配置。传 None 的字段保持原值。

    默认关闭是既有设计：主动消息是打扰，必须由用户明确开启。
    """
    slug = _resolve_slug(slug, user_id)

    from core.proactive import set_config

    try:
        config = set_config(
            slug,
            owner=user_id,
            enabled=enabled,
            max_per_day=max_per_day,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
        )
    except ValueError as e:
        raise ServiceError("invalid_config", str(e)) from None
    return {"slug": slug, "config": config}


def proactive_tick(user_id: int, slug: str) -> dict:
    """判定此刻是否该主动发消息；该发则生成并排队。

    生成走同一个引擎，人格与时间感知才与日常对话一致。
    这是唯一会调用大模型的 proactive 工具，客户端应按低频节奏调用
    （既有 cron 建议每小时一次）——判定的日上限虽能兜住，但调用本身有成本。
    """
    slug = _resolve_slug(slug, user_id)

    from core.conversation_store import load_jsonl_messages
    from core.proactive import (
        compose_with_usage,
        decide_trigger,
        get_config,
        queue_message,
    )
    from server.billing_gate import QuotaBlocked, release, reserve, settle
    from server.safety_gate import check_output

    history = load_jsonl_messages(slug, user_id)
    last_seen = history[-1].get("created_at") if history else None

    trigger = decide_trigger(slug, user_id, owner=user_id, last_seen_iso=last_seen)
    config = get_config(slug, owner=user_id)

    if trigger is None:
        blocked = _blocked_reason(user_id)
        return {
            "sent": False,
            "trigger": None,
            "message_id": None,
            "content": "",
            "config": config,
            "blocked_reason": blocked,
            "reason": "健康保护生效，主动消息被跳过" if blocked else _NO_TRIGGER_REASON,
        }

    _check_ready(slug, user_id)
    engine = _get_engine(slug, user_id)
    result = {
        "sent": False,
        "trigger": trigger,
        "message_id": None,
        "content": "",
        "config": config,
        "blocked_reason": None,
        "reason": "",
    }
    try:
        account_id, _outcome = reserve(user_id)
    except QuotaBlocked as e:
        result.update(notice=e.notice, reason=str(e))
        return result

    try:
        content, usage = compose_with_usage(engine, trigger)
        if not content.strip():
            release(account_id)
            result["reason"] = "本次生成内容为空，未排队"
            return result
        blocked = check_output(user_id, slug, content)
        if blocked is not None:
            release(account_id)
            result.update(notice=blocked, reason=blocked.get("message", "输出被拦截"))
            return result

        message_id = queue_message(slug, user_id, trigger, content, owner=user_id)
        if message_id is None:
            release(account_id)
            result["reason"] = "本次生成内容为空，未排队"
            return result
        settle(
            account_id,
            usage,
            user_id=user_id,
            slug=slug,
            provider=getattr(engine, "last_provider", ""),
            model=getattr(engine, "model", ""),
            kind="proactive",
        )
    except Exception as e:  # noqa: BLE001 — 生成失败要翻译成用户能懂的原因
        release(account_id)
        logger.warning("生成主动消息失败 user=%s slug=%s: %s", user_id, slug, e)
        raise ServiceError(
            "model_unavailable",
            "无法生成主动消息：请先在网页「模型设置」里保存自己的 LLM Key。",
        ) from None

    result.update(sent=True, message_id=message_id, content=content)
    return result


def proactive_ack(user_id: int, message_ids: list) -> dict:
    """把已成功投递的消息标记为 delivered。

    mark_delivered 自带 user_id 校验，改不到别人的消息。
    仍在此处做类型与数量校验：MCP 的调用方是大模型，
    不能指望它一定传来一个干净的整数数组。
    """
    from core.proactive import mark_delivered

    if not isinstance(message_ids, list):
        raise ServiceError("invalid_argument", "message_ids 必须是整数数组")
    if len(message_ids) > 100:
        raise ServiceError("invalid_argument", "一次最多确认 100 条消息")

    ids = []
    for raw in message_ids:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            raise ServiceError(
                "invalid_argument", f"message_ids 含非法项: {raw!r}"
            ) from None

    return {
        "requested": len(ids),
        "acknowledged": mark_delivered(ids, user_id),
    }
