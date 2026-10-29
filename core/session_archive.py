"""会话归档共用服务：原始对话归档 + LLM 语义摘要 + SKILL.md 记忆段更新。

从 CLI 的 ChatSession 提取为独立模块，Web 与 CLI 共用同一套逻辑；
函数签名不依赖 CLI 的 Session 对象。
"""

import json
import fcntl
import hashlib
import logging
from datetime import datetime
from typing import Optional

from config import ARCHIVE_THRESHOLD, PROJECT_DIR
from core.mirror_store import mirror_store

logger = logging.getLogger("ex-memory")

_PROMPTS_DIR = PROJECT_DIR / "prompts"
_STATE_FILENAME = "archive_state.json"


def maybe_archive(
    slug: str,
    vector_store=None,
    embedder=None,
    threshold: int = ARCHIVE_THRESHOLD,
    owner: Optional[int] = None,
    conversation_id: Optional[str] = None,
) -> bool:
    """检查未归档轮数，达到阈值则归档一次会话。

    通过 archive_state.json 的读-改-写文件锁原子「认领」归档区间，
    并发请求同时到达时只有一个会真正归档。认领先于慢操作（LLM 摘要）：
    中途失败只是跳过该窗口的摘要，原始对话仍在 conversation.jsonl 不丢。

    Returns:
        是否触发了归档
    """
    if conversation_id is not None:
        return _maybe_archive_conversation(slug, owner, conversation_id, threshold)
    store = mirror_store(slug, owner)

    def _claim(state: dict) -> Optional[dict]:
        from core.conversation_store import load_jsonl_messages

        # 旧全局摘要只兼容网页记录，不能把新 IM 独立会话提升为共享人物记忆。
        messages = load_jsonl_messages(slug, owner, conversation_id="web")
        total = len(messages)
        archived = int(state.get("archived_messages", 0))
        # 对话文件可能被留存策略清理而变短，重新对齐
        if archived > total:
            archived = total
        pending = messages[archived:total]
        turns = sum(1 for m in pending if m.get("role") == "user")
        if turns < threshold:
            return None
        state["archived_messages"] = total
        state["updated_at"] = datetime.now().isoformat()
        return {"start": archived, "end": total, "turns": turns}

    try:
        claim = store.locked_update_json(f"sessions/{_STATE_FILENAME}", dict, _claim)
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("会话归档认领失败 slug=%s: %s", slug, e)
        return False

    if claim is None:
        return False

    from core.conversation_store import load_jsonl_messages

    pending = load_jsonl_messages(slug, owner, conversation_id="web")[
        claim["start"] : claim["end"]
    ]
    try:
        archive_session(
            slug,
            pending,
            vector_store=vector_store,
            embedder=embedder,
            owner=owner,
        )
        return True
    except (OSError, ValueError) as e:
        # 归档失败不影响对话主流程；区间已认领，下个窗口继续
        logger.warning("会话归档失败 slug=%s: %s", slug, e)
        return False


def archive_session(
    slug: str,
    messages: list[dict],
    vector_store=None,
    embedder=None,
    engine=None,
    owner: Optional[int] = None,
) -> Optional[dict]:
    """归档一次会话：写原始记录 → 生成 LLM 摘要 → 更新 SKILL.md 记忆段。

    Args:
        slug: 镜像 slug
        messages: [{role, content}] 列表
        vector_store / embedder: 可选，用于把摘要写入向量库
        engine: 可选，CLI 传入以即时更新 engine.session_summaries
        owner: 多用户模式下镜像归属账号，用于定位按账号隔离的目录

    Returns:
        {"session_file": 归档文件名, "summary": 摘要文本或 None}；
        messages 为空时返回 None
    """
    if not messages:
        return None

    store = mirror_store(slug, owner)
    store.mkdir("sessions")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_file_rel = f"sessions/session_{timestamp}.md"
    session_file_name = session_file_rel.rsplit("/", 1)[-1]

    lines = [f"# 对话记录 — {timestamp}\n"]
    for msg in messages:
        role = "用户" if msg["role"] == "user" else slug
        lines.append(f"**{role}**: {msg['content']}\n")

    store.write_text(session_file_rel, "\n".join(lines))
    logger.info("对话已归档: %s", session_file_name)

    summary = _generate_summary(
        slug,
        timestamp,
        messages,
        vector_store,
        embedder,
        engine,
        owner,
    )
    return {"session_file": session_file_name, "summary": summary}


def _generate_summary(
    slug: str,
    timestamp: str,
    messages: list[dict],
    vector_store,
    embedder,
    engine,
    owner: Optional[int] = None,
) -> Optional[str]:
    """调用 LLM 生成会话语义摘要，用于下次启动时快速恢复上下文。"""
    from config import get_llm_config, get_llm_client

    store = mirror_store(slug, owner)
    cfg = get_llm_config(owner) if owner is not None else get_llm_config()
    if not cfg["api_key"]:
        return None

    try:
        prompt_template = (_PROMPTS_DIR / "session_summary.md").read_text(
            encoding="utf-8"
        )

        # 只取最近 40 条消息做摘要（避免上下文超限）
        recent = messages[-40:]
        history_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else slug}: {m['content'][:300]}"
            for m in recent
        )

        client = get_llm_client(owner, cfg) if owner is not None else get_llm_client()
        response = client.chat.completions.create(
            model=cfg["model"],
            messages=[
                {"role": "system", "content": prompt_template},
                {
                    "role": "user",
                    "content": f"请压缩以下对话为摘要：\n\n{history_text}",
                },
            ],
            temperature=0.3,
        )
        summary = response.choices[0].message.content

        summary_file_rel = f"sessions/session_{timestamp}_summary.md"
        summary_file_name = summary_file_rel.rsplit("/", 1)[-1]
        store.write_text(summary_file_rel, summary)
        logger.info("会话摘要已生成: %s", summary_file_name)

        # 登记进记忆索引并跑一次衰减（FR-068）。
        # 衰减不是删除——久远的低重要度记忆会变模糊，那比全都记得更像人。
        try:
            from core.memory_decay import (
                MemoryIndex,
                MEMORY_INDEX_FILE,
                run_decay_cycle,
            )

            index = MemoryIndex(store.path(MEMORY_INDEX_FILE))
            index.add(summary_file_name, summary, source="session")
            index.save()
            run_decay_cycle(store.path(""))
        except Exception as e:  # noqa: BLE001 — 记忆衰减是锦上添花，不该拖垮归档
            logger.warning("记忆索引登记失败: %s", e)

        # 追加到引擎的 session_summaries（当前会话可能还没结束，但预先加载）
        if engine is not None:
            engine.session_summaries.append(summary)
            # Token 预算控制：过大的摘要列表弹出旧项
            from config import LLM_MAX_CONTEXT_CHARS
            from core.validation import estimate_tokens

            while (
                len(engine.session_summaries) > 5
                and estimate_tokens("\n".join(engine.session_summaries))
                > LLM_MAX_CONTEXT_CHARS * 0.3
            ):
                engine.session_summaries.pop(0)

        # 可选：加入向量库
        if vector_store and embedder:
            try:
                vector_store.add_session_summary(summary, slug, embedder)
            except Exception:
                logger.debug("摘要写入向量库失败（非关键）")

        # 同步更新 SKILL.md 的记忆段
        update_skill_memory(slug, summary, owner)
        return summary
    except Exception as e:
        # 与 CLI 原行为一致：摘要失败降级，原始归档完好，只告警不中断
        logger.warning("生成会话摘要失败（已降级，原始归档完好）: %s", e)
        return None


def update_skill_memory(
    slug: str, new_summary: str, owner: Optional[int] = None
) -> None:
    """将新摘要追加到 SKILL.md 的 PART A 末尾（带文件锁的读-改-写）。"""
    store = mirror_store(slug, owner)
    if not store.exists("SKILL.md"):
        return

    marker = "---\n\n## PART B"

    try:
        store.locked_update_text(
            "SKILL.md", lambda content: _insert_summary(content, marker, new_summary)
        )
        logger.info("SKILL.md 已同步最新摘要")
    except OSError as e:
        logger.debug("更新 SKILL.md 摘要失败（非关键）: %s", e)


def _insert_summary(content: str, marker: str, new_summary: str) -> str:
    """在 PART B 标记前插入摘要段；无标记时不改动。"""
    if marker not in content:
        return content
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    addition = f"\n\n### 对话摘要 ({timestamp})\n{new_summary}\n"
    return content.replace(marker, addition + marker)


def _conversation_directory(conversation_id: str) -> str:
    from core.conversation_store import conversation_key

    return f"sessions/conversations/{conversation_key(conversation_id)}"


def _read_conversation_state(store, directory: str) -> dict:
    rel = f"{directory}/state.json"
    if not store.exists(rel):
        return {}
    state = store.read_json(rel)
    if not isinstance(state, dict):
        raise ValueError("会话归档状态无效")
    return state


def _pending_conversation_messages(messages: list[dict], state: dict) -> list[dict]:
    last_id = state.get("last_message_id")
    if last_id:
        for index, message in enumerate(messages):
            if message.get("id") == last_id:
                return messages[index + 1 :]
        # 留存清理可以删掉游标前的记录；锚点不在时不按旧下标跳过新消息。
    return messages


def _maybe_archive_conversation(
    slug: str, owner: Optional[int], conversation_id: str, threshold: int
) -> bool:
    from core.conversation_store import load_jsonl_messages

    if type(threshold) is not int or threshold < 1:
        raise ValueError("归档轮数阈值无效")
    directory = _conversation_directory(conversation_id)
    store = mirror_store(slug, owner)
    store.mkdir(directory)
    # 慢调用期间也持有独立非阻塞锁；崩溃会由 OS 释放，无需猜测租约时长。
    with open(store.path(f"{directory}/archive.lock"), "a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            state = _read_conversation_state(store, directory)
            batch_id = state.get("pending_batch")
            if batch_id:
                if not isinstance(batch_id, str) or not _valid_batch_id(batch_id):
                    raise ValueError("归档批次标识无效")
                batch = store.read_json(f"{directory}/{batch_id}.json")
                pending = batch["messages"]
            else:
                messages = load_jsonl_messages(slug, owner, conversation_id)
                pending = _pending_conversation_messages(messages, state)
                if sum(m.get("role") == "user" for m in pending) < threshold:
                    return False
                # 每批最多二十轮，避免一次积压让早期内容被摘要窗口静默丢掉。
                pending = pending[:40]
                if pending and pending[-1].get("role") == "user":
                    pending = pending[:-1]
                if not pending:
                    return False
                encoded = json.dumps(pending, ensure_ascii=False, sort_keys=True)
                batch_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                batch = {
                    "batch_id": batch_id,
                    "conversation_id": conversation_id,
                    "created_at": datetime.now().isoformat(),
                    "kind": "generated_conversation",
                    "messages": pending,
                }
                store.write_json(f"{directory}/{batch_id}.json", batch)
                state.update(pending_batch=batch_id, batch_id=batch_id)
            state.update(status="running", last_error="")
            store.write_json(f"{directory}/state.json", state)
            try:
                summary_rel = f"{directory}/{batch_id}.summary.json"
                if store.exists(summary_rel):
                    summary_record = store.read_json(summary_rel)
                    if (
                        not isinstance(summary_record.get("text"), str)
                        or not summary_record["text"].strip()
                    ):
                        raise ValueError("已保存摘要无效")
                else:
                    summary = _generate_conversation_summary(slug, owner, pending)
                    summary_record = {
                        "id": batch_id,
                        "text": summary,
                        "source": "session_summary",
                        "kind": "generated_summary",
                        "created_at": batch["created_at"],
                        "conversation_id": conversation_id,
                    }
                    store.write_json(summary_rel, summary_record)
                from core.memory_state import touch_memory

                touch_memory(slug, owner)
                completed = dict(state)
                completed.update(
                    status="complete",
                    pending_batch=None,
                    last_message_id=pending[-1]["id"],
                    archived_messages=int(state.get("archived_messages", 0))
                    + len(pending),
                    updated_at=datetime.now().isoformat(),
                    last_error="",
                )
                store.write_json(f"{directory}/state.json", completed)
                return True
            except Exception as exc:  # noqa: BLE001 — 留下原始批次，由下次请求重试
                state.update(status="failed", last_error="摘要未完成，下次对话可重试")
                store.write_json(f"{directory}/state.json", state)
                logger.warning("会话摘要失败 slug=%s kind=%s", slug, type(exc).__name__)
                return False
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _valid_batch_id(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _generate_conversation_summary(
    slug: str, owner: Optional[int], messages: list[dict]
) -> str:
    from config import get_llm_config, get_llm_client

    cfg = get_llm_config(owner) if owner is not None else get_llm_config()
    if not cfg.get("api_key"):
        raise ValueError("未配置摘要模型")
    prompt = (_PROMPTS_DIR / "session_summary.md").read_text(encoding="utf-8")
    prompt += (
        "\n本材料是用户与记忆体的生成对话。AI 回复不是原始人物说过的话。"
        "只为本会话保留话题与用户明确表达的信息，不把 AI 推测写成新增人物事实。"
    )
    history = "\n".join(
        f"{'用户' if m['role'] == 'user' else 'AI生成回复'}: {m['content'][:1000]}"
        for m in messages
    )
    client = get_llm_client(owner, cfg) if owner is not None else get_llm_client()
    response = client.chat.completions.create(
        model=cfg["model"],
        temperature=0.3,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": history},
        ],
    )
    summary = response.choices[0].message.content
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 16000:
        raise ValueError("摘要内容无效")
    return summary.strip()


def load_conversation_summaries(
    slug: str, owner: Optional[int], conversation_id: str
) -> list[dict]:
    directory = _conversation_directory(conversation_id)
    store = mirror_store(slug, owner)
    records = []
    for rel in store.list(directory, "*.summary.json"):
        record = store.read_json(rel)
        if (
            isinstance(record, dict)
            and record.get("conversation_id") == conversation_id
            and isinstance(record.get("text"), str)
            and record["text"].strip()
        ):
            records.append(record)
    return sorted(
        records, key=lambda item: (item.get("created_at", ""), item.get("id", ""))
    )[-3:]


def get_archive_status(
    slug: str, owner: Optional[int], conversation_id: str = "web"
) -> dict:
    from core.conversation_store import load_jsonl_messages

    directory = _conversation_directory(conversation_id)
    store = mirror_store(slug, owner)
    state = _read_conversation_state(store, directory)
    pending = _pending_conversation_messages(
        load_jsonl_messages(slug, owner, conversation_id), state
    )
    pending_turns = sum(message["role"] == "user" for message in pending)
    status = state.get("status", "idle")
    if pending_turns and status in ("idle", "complete"):
        status = "pending"
    return {
        "status": status,
        "pending_turns": pending_turns,
        "archived_messages": int(state.get("archived_messages", 0)),
        "last_error": state.get("last_error", ""),
        "batch_id": state.get("batch_id"),
    }
