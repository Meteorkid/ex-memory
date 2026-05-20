"""ChatEngine：SKILL.md + RAG 动态注入 + 重试 + Token 预算。"""

import re
import logging
from typing import Optional

from config import (
    get_llm_config, get_llm_client, get_ex_dir, RECENT_SESSIONS, DEFAULT_TOP_K,
    RAG_THRESHOLD, LLM_MAX_CONTEXT_CHARS,
)
from core.retry import retry_api
from core.validation import estimate_tokens, sanitize_chat_history
from core.sticker_selector import select_stickers, IMAGE_STICKERS

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
        self._rag_failures = 0
        self._rag_recovery_interval = 5  # 降级后每隔 N 轮尝试恢复一次
        self._turn_since_last_rag_attempt = 0

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
        sticker_list = ", ".join(f"{sid}({s['label']})" for sid, s in IMAGE_STICKERS.items())

        # Token 预算：仅截断 session 摘要副本
        summaries = list(self.session_summaries)
        budget = int(LLM_MAX_CONTEXT_CHARS * 0.5)

        def _assemble(sums: list[str]) -> str:
            p = [self.skill_content]
            if sums:
                p.append("\n---\n## 最近对话记忆\n")
                for i, summary in enumerate(sums, 1):
                    p.append(f"### 第 {i} 次\n{summary}\n")
            if self.corrections.strip():
                p.append(f"\n---\n## 用户纠正记录（优先级最高）\n{self.corrections}\n")
            if rag_results:
                filtered = [r for r in rag_results if r.get("score", 0) > RAG_THRESHOLD]
                if filtered:
                    p.append("\n---\n## 潜意识层 — ta 在类似场景下真实说过的话\n")
                    p.append("以下是从聊天记录中检索到的 ta 的原话，作为你回复的语气锚点：\n")
                    for r in filtered:
                        p.append(f"- {r.get('display_text', '')}")
                    p.append("\n请以这些原话的语气、标点习惯、断句方式为参考来回复。\n")
            p.append(
                f"\n---\n## 可用图片表情包\n你可以在回复中使用图片表情包来表达情绪。"
                f"在回复文本末尾加上 [sticker:贴纸ID] 标记即可。\n可用贴纸：{sticker_list}\n"
                f"示例：哈哈哈 [sticker:builtin_happy_laugh]\n"
            )
            return "\n".join(p)

        while len(summaries) > 1 and estimate_tokens(_assemble(summaries)) > budget:
            logger.warning("System prompt 过大，截断 session 摘要")
            if len(summaries) > 2:
                summaries.pop(1)
            else:
                summaries = summaries[-1:]
                break

        return _assemble(summaries)

    def _is_rag_degraded(self) -> bool:
        """连续 3 次失败后进入降级模式。"""
        return self._rag_failures >= 3

    def _rag_search(self, user_input: str) -> list[dict]:
        if not self.vector_store or not self.embedder:
            return []

        # 降级模式下每隔 N 轮尝试一次恢复
        if self._is_rag_degraded():
            self._turn_since_last_rag_attempt += 1
            if self._turn_since_last_rag_attempt < self._rag_recovery_interval:
                return []
            self._turn_since_last_rag_attempt = 0
            logger.info("尝试恢复 RAG 检索 (failures=%d)", self._rag_failures)

        try:
            results = self.vector_store.search_target_only(
                query=user_input, embedder=self.embedder, top_k=DEFAULT_TOP_K
            )
            # 成功 — 重置失败计数
            if self._rag_failures > 0:
                logger.info("RAG 检索已恢复")
            self._rag_failures = 0
            self._turn_since_last_rag_attempt = 0
            return results
        except Exception:
            self._rag_failures += 1
            msg = f"RAG 检索失败 ({self._rag_failures}/3)"
            if self._is_rag_degraded():
                logger.warning(msg + "，进入降级模式", exc_info=True)
            else:
                logger.warning(msg, exc_info=True)
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
        messages.extend(sanitize_chat_history(history))
        messages.append({"role": "user", "content": user_input})
        return messages

    @staticmethod
    def _extract_sticker_tags(text: str) -> tuple[str, list[str]]:
        """从回复文本中提取 [sticker:xxx] 标记，返回 (清理后文本, 贴纸ID列表)。"""
        pattern = r'\[sticker:([a-zA-Z0-9_-]+)\]'
        sticker_ids = re.findall(pattern, text)
        clean_text = re.sub(pattern, '', text).strip()
        return clean_text, sticker_ids

    def chat(self, user_input: str, history: list[dict]) -> tuple[str, list[str], object]:
        messages = self._prepare_messages(user_input, history)

        response = self._call_api(messages)
        reply = response.choices[0].message.content or ""
        reply, inline_stickers = self._extract_sticker_tags(reply)
        # 情绪分析选择的贴纸
        stickers = select_stickers(reply)
        seen = set()
        all_stickers = []
        for sid in inline_stickers + stickers:
            if sid not in seen:
                seen.add(sid)
                all_stickers.append(sid)
        return reply, all_stickers, response.usage

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

        clean_reply, inline_stickers = self._extract_sticker_tags(full_reply)
        stickers = select_stickers(clean_reply)
        seen = set()
        for sid in inline_stickers + stickers:
            if sid in seen:
                continue
            seen.add(sid)
            yield {"type": "sticker", "id": sid}

        # 检测是否触发红包
        from core.wallet_manager import detect_redpacket_trigger, create_redpacket
        trigger = detect_redpacket_trigger(user_input, full_reply)
        if trigger:
            rp = create_redpacket(self.slug, trigger)
            if rp:
                yield {"type": "red_packet", "id": rp["id"], "amount": rp["amount"], "note": rp["note"]}
