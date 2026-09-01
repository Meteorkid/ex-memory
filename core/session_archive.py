"""会话归档共用服务：原始对话归档 + LLM 语义摘要 + SKILL.md 记忆段更新。

从 CLI 的 ChatSession 提取为独立模块，Web 与 CLI 共用同一套逻辑；
函数签名不依赖 CLI 的 Session 对象。
"""

import fcntl
import json
import logging
from datetime import datetime
from typing import Optional

from config import resolve_ex_dir, ARCHIVE_THRESHOLD, PROJECT_DIR
from core.file_utils import atomic_write, locked_update_json, _lock

logger = logging.getLogger("ex-memory")

_PROMPTS_DIR = PROJECT_DIR / "prompts"
_STATE_FILENAME = "archive_state.json"


def maybe_archive(
    slug: str,
    vector_store=None,
    embedder=None,
    threshold: int = ARCHIVE_THRESHOLD,
    owner: Optional[int] = None,
) -> bool:
    """检查未归档轮数，达到阈值则归档一次会话。

    通过 archive_state.json 的读-改-写文件锁原子「认领」归档区间，
    并发请求同时到达时只有一个会真正归档。认领先于慢操作（LLM 摘要）：
    中途失败只是跳过该窗口的摘要，原始对话仍在 conversation.jsonl 不丢。

    Returns:
        是否触发了归档
    """
    state_path = resolve_ex_dir(slug, owner) / "sessions" / _STATE_FILENAME

    def _claim(state: dict) -> Optional[dict]:
        from core.conversation_store import load_jsonl_messages

        messages = load_jsonl_messages(slug, owner)
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
        claim = locked_update_json(state_path, dict, _claim)
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("会话归档认领失败 slug=%s: %s", slug, e)
        return False

    if claim is None:
        return False

    from core.conversation_store import load_jsonl_messages

    pending = load_jsonl_messages(slug, owner)[claim["start"] : claim["end"]]
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

    sessions_dir = resolve_ex_dir(slug, owner) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_file = sessions_dir / f"session_{timestamp}.md"

    lines = [f"# 对话记录 — {timestamp}\n"]
    for msg in messages:
        role = "用户" if msg["role"] == "user" else slug
        lines.append(f"**{role}**: {msg['content']}\n")

    atomic_write(session_file, "\n".join(lines))
    logger.info("对话已归档: %s", session_file.name)

    summary = _generate_summary(
        slug,
        sessions_dir,
        timestamp,
        messages,
        vector_store,
        embedder,
        engine,
        owner,
    )
    return {"session_file": session_file.name, "summary": summary}


def _generate_summary(
    slug: str,
    sessions_dir,
    timestamp: str,
    messages: list[dict],
    vector_store,
    embedder,
    engine,
    owner: Optional[int] = None,
) -> Optional[str]:
    """调用 LLM 生成会话语义摘要，用于下次启动时快速恢复上下文。"""
    from config import get_llm_config, get_llm_client

    cfg = get_llm_config()
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

        client = get_llm_client()
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

        summary_file = sessions_dir / f"session_{timestamp}_summary.md"
        summary_file.write_text(summary, encoding="utf-8")
        logger.info("会话摘要已生成: %s", summary_file.name)

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
    skill_path = resolve_ex_dir(slug, owner) / "SKILL.md"
    if not skill_path.exists():
        return

    lock_path = skill_path.with_name(skill_path.name + ".lock")
    try:
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            _lock(lock_file, fcntl.LOCK_EX)
            content = skill_path.read_text(encoding="utf-8")
            marker = "---\n\n## PART B"
            if marker in content:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                addition = f"\n\n### 对话摘要 ({timestamp})\n{new_summary}\n"
                atomic_write(skill_path, content.replace(marker, addition + marker))
                logger.info("SKILL.md 已同步最新摘要")
    except OSError as e:
        logger.debug("更新 SKILL.md 摘要失败（非关键）: %s", e)
