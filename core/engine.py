"""ChatEngine：加载 SKILL.md + RAG 动态注入，调用 OpenAI 兼容 API。"""

from typing import Optional
from pathlib import Path
from openai import OpenAI
from config import get_llm_config, get_ex_dir, RECENT_SESSIONS, DEFAULT_TOP_K


class ChatEngine:
    def __init__(self, slug: str, vector_store=None, embedder=None):
        cfg = get_llm_config()
        self.client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
        self.model = cfg["model"]
        self.temperature = cfg["temperature"]
        self.top_p = cfg["top_p"]
        self.frequency_penalty = cfg["frequency_penalty"]

        self.slug = slug
        self.ex_dir = get_ex_dir(slug)
        self.vector_store = vector_store
        self.embedder = embedder

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

        # 加载最近 N 次 session 摘要
        sessions_dir = self.ex_dir / "sessions"
        if sessions_dir.exists():
            session_files = sorted(sessions_dir.glob("*.md"), reverse=True)[:RECENT_SESSIONS]
            self.session_summaries = [f.read_text(encoding="utf-8") for f in session_files]

        # 加载纠正记录
        corrections_path = self.ex_dir / "corrections.md"
        if corrections_path.exists():
            self.corrections = corrections_path.read_text(encoding="utf-8")

        print(f"--- 已连接到 {self.slug} 的数字镜像 ---")

    def _build_system_prompt(self, rag_results: Optional[list[dict]] = None) -> str:
        """构造 system prompt：SKILL.md + session 摘要 + corrections + RAG 结果。"""
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
                if score > 0.3:  # 相关度阈值
                    parts.append(f"- {text}")
            parts.append("\n请以这些原话的语气、标点习惯、断句方式为参考来回复。\n")

        return "\n".join(parts)

    def _rag_search(self, user_input: str) -> list[dict]:
        """从向量库检索 ta 的原话。"""
        if not self.vector_store or not self.embedder:
            return []
        try:
            return self.vector_store.search_target_only(
                query=user_input, embedder=self.embedder, top_k=DEFAULT_TOP_K
            )
        except Exception:
            return []

    def chat(self, user_input: str, history: list[dict]) -> tuple[str, object]:
        """发送消息并获取回复。

        Returns:
            (reply_content, usage)
        """
        # RAG 检索
        rag_results = self._rag_search(user_input)

        # 构造消息
        system_prompt = self._build_system_prompt(rag_results)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_input})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            top_p=self.top_p,
            frequency_penalty=self.frequency_penalty,
            stream=False,
        )

        return response.choices[0].message.content, response.usage
