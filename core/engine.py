"""ChatEngine：SKILL.md + RAG 动态注入 + 重试 + Token 预算。"""

import logging
from typing import Optional
from pathlib import Path

from config import (
    get_llm_config, get_llm_client, get_ex_dir, RECENT_SESSIONS, DEFAULT_TOP_K,
    RAG_THRESHOLD, LLM_MAX_CONTEXT_CHARS,
)
from core.retry import retry_api
from core.validation import estimate_tokens
from core.sticker_selector import select_stickers

logger = logging.getLogger("ex-memory")


class ChatEngine:
    def __init__(self, slug: str, vector_store=None, embedder=None):
        cfg = get_llm_config()
        self.client = get_llm_client()
        self.model = cfg["model"]
        self.temperature = cfg["temperature"]
        self.top_p = cfg["top_p"]
        self.frequency_penalty = cfg["frequency_penalty"]
        self.max_tokens = cfg["max_tokens"]

        self.slug = slug
        self.ex_dir = get_ex_dir(slug)
        self.vector_store = vector_store
        self.embedder = embedder
        self._rag_healthy = True

        self.skill_content = ""
        self.session_summaries = []
        self.corrections = ""

        self._load()

    def _load(self):
        """加载 SKILL.md、session 摘要、corrections。"""
        skill_path = self.ex_dir / "SKILL.md"
        if not skill_path.exists():
            raise FileNotFoundError(f"缺少镜像文件: {skill_path}")
        self.skill_content = skill_path.read_text(encoding="utf-8")

        sessions_dir = self.ex_dir / "sessions"
        if sessions_dir.exists():
            # 优先使用 LLM 语义摘要（短小精准），没有则回退到原始归档
            summary_files = sorted(sessions_dir.glob("*_summary.md"), reverse=True)[:RECENT_SESSIONS]
            if summary_files:
                self.session_summaries = [f.read_text(encoding="utf-8") for f in summary_files]
            else:
                # 兼容旧归档（无摘要文件时直接读原始对话）
                raw_files = sorted(sessions_dir.glob("session_*.md"), reverse=True)[:RECENT_SESSIONS]
                self.session_summaries = [f.read_text(encoding="utf-8") for f in raw_files]

        corrections_path = self.ex_dir / "corrections.md"
        if corrections_path.exists():
            self.corrections = corrections_path.read_text(encoding="utf-8")

        logger.info("已连接 %s 的数字镜像 (model=%s)", self.slug, self.model)

    def _build_system_prompt(self, rag_results: Optional[list[dict]] = None) -> str:
        parts = [self.skill_content]

        if self.session_summaries:
            parts.append("\n---\n## 最近对话记忆\n")
            for i, summary in enumerate(self.session_summaries, 1):
                parts.append(f"### 第 {i} 次\n{summary}\n")

        if self.corrections.strip():
            parts.append(f"\n---\n## 用户纠正记录（优先级最高）\n{self.corrections}\n")

        if rag_results:
            parts.append("\n---\n## 潜意识层 — ta 在类似场景下真实说过的话\n")
            parts.append("以下是从聊天记录中检索到的 ta 的原话，作为你回复的语气锚点：\n")
            for r in rag_results:
                score = r.get("score", 0)
                text = r.get("display_text", "")
                if score > RAG_THRESHOLD:
                    parts.append(f"- {text}")
            parts.append("\n请以这些原话的语气、标点习惯、断句方式为参考来回复。\n")

        prompt = "\n".join(parts)

        # Token 预算控制（操作副本，不破坏 self.session_summaries）
        tokens = estimate_tokens(prompt)
        if tokens > LLM_MAX_CONTEXT_CHARS * 0.5:
            logger.warning("System prompt 过大 (%d tokens)，截断 session 摘要", tokens)
            summaries = list(self.session_summaries)
            while summaries and estimate_tokens(prompt) > LLM_MAX_CONTEXT_CHARS * 0.5:
                summaries.pop(0)
                prompt = "\n".join([self.skill_content] + [
                    f"\n---\n## 最近对话记忆\n" + "\n".join(
                        f"### 第 {i} 次\n{s}" for i, s in enumerate(summaries, 1)
                    )
                ])

        return prompt

    def _rag_search(self, user_input: str) -> list[dict]:
        if not self.vector_store or not self.embedder or not self._rag_healthy:
            return []
        try:
            return self.vector_store.search_target_only(
                query=user_input, embedder=self.embedder, top_k=DEFAULT_TOP_K
            )
        except Exception:
            logger.warning("RAG 检索失败，降级为纯文本模式", exc_info=True)
            self._rag_healthy = False
            return []

    @retry_api(max_attempts=3, base_delay=1.0)
    def _call_api(self, messages: list[dict]):
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            top_p=self.top_p,
            frequency_penalty=self.frequency_penalty,
            max_tokens=self.max_tokens,
            stream=False,
        )

    def _prepare_messages(self, user_input: str, history: list[dict]) -> list[dict]:
        """构建完整的消息列表（system + history + user）。"""
        rag_results = self._rag_search(user_input)
        system_prompt = self._build_system_prompt(rag_results)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[-100:])
        messages.append({"role": "user", "content": user_input})
        return messages

    def chat(self, user_input: str, history: list[dict]) -> tuple[str, list[str], object]:
        messages = self._prepare_messages(user_input, history)

        response = self._call_api(messages)
        reply = response.choices[0].message.content
        stickers = select_stickers(reply)
        return reply, stickers, response.usage

    def chat_stream(self, user_input: str, history: list[dict]):
        """流式对话，yield dict: {type: text|sticker, content/id: ...}"""
        messages = self._prepare_messages(user_input, history)

        full_reply = ""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            top_p=self.top_p,
            frequency_penalty=self.frequency_penalty,
            max_tokens=self.max_tokens,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                full_reply += delta.content
                yield {"type": "text", "content": delta.content}

        # 流式结束后，选择贴纸
        stickers = select_stickers(full_reply)
        for sid in stickers:
            yield {"type": "sticker", "id": sid}

        # 检测是否触发红包
        from core.wallet_manager import detect_redpacket_trigger, create_redpacket
        trigger = detect_redpacket_trigger(user_input, full_reply)
        if trigger:
            rp = create_redpacket(self.slug, trigger)
            if rp:
                yield {"type": "red_packet", "id": rp["id"], "amount": rp["amount"], "note": rp["note"]}
