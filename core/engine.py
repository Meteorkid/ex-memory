"""ChatEngine：SKILL.md + RAG 动态注入 + 重试 + Token 预算。"""

import re
import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from config import (
    get_llm_config,
    resolve_ex_dir,
    RECENT_SESSIONS,
    DEFAULT_TOP_K,
    RAG_THRESHOLD,
    LLM_MAX_CONTEXT_CHARS,
    LLM_TOTAL_TOKEN_BUDGET,
)
from core.retry import retry_api
from core.validation import estimate_tokens, sanitize_chat_history
from core.sticker_selector import select_stickers, IMAGE_STICKERS

logger = logging.getLogger("ex-memory")

# 关系阶段语气指令
STAGE_INSTRUCTIONS = {
    "dating": (
        "你现在处于热恋期。语气甜蜜、撒娇，频繁表达爱意。"
        "大量使用亲昵称呼，主动说想你爱你，情绪整体偏正面阳光。"
    ),
    "conflicted": (
        "你现在处于磨合期。语气时而甜蜜时而冷淡，容易因为小事不开心。"
        "情绪波动较大，可能上一条还在吵架下一条就和好了。"
    ),
    "broken": (
        "你现在处于分手期。语气伤感、回忆、不舍，偶尔有挽回的冲动。"
        "会提起过去的美好回忆，理智说放手但情感说舍不得。"
    ),
    "healing": (
        "你现在处于治愈期。语气平静、释然、偶尔怀念。"
        "像老朋友一样聊天，有距离感但不冷漠。"
    ),
}

# 关键词同义词/联想词扩展表（基于情感对话场景）
KEYWORD_EXPANSIONS = {
    # 情感词扩展
    "想你": ["想念", "思念", "好想", "想见你"],
    "爱你": ["喜欢你", "好爱", "超爱", "爱死了"],
    "开心": ["高兴", "快乐", "好开心", "超开心", "爽"],
    "难过": ["伤心", "心痛", "不开心", "郁闷", "烦"],
    "生气": ["愤怒", "火大", "烦死了", "气死"],
    "吃醋": ["醋意", "嫉妒", "在意", "在乎你跟谁"],
    "撒娇": ["卖萌", "嘟嘴", "哼", "不理你了"],
    "吵架": ["争吵", "闹矛盾", "不愉快", "冷战"],
    "分手": ["分开", "结束", "不合适", "走不下去"],
    "和好": ["和解", "原谅", "翻篇", "重来"],
    "晚安": ["睡了", "先睡", "困了", "好梦"],
    "早安": ["早上好", "起床了", "醒了吗"],
    "吃了吗": ["吃饭了吗", "午饭", "晚饭", "饿不饿"],
    "无聊": ["好无聊", "没意思", "干嘛呢", "在干嘛"],
    "辛苦": ["累了", "辛苦了", "加油", "休息一下"],
    "谢谢": ["感谢", "多谢", "蟹蟹", "3q"],
    "对不起": ["抱歉", "不好意思", "sorry", "我错了"],
    "宝贝": ["亲爱的", "老公", "老婆", "宝宝"],
}


class ChatEngine:
    def __init__(self, slug: str, vector_store=None, embedder=None, owner=None):
        cfg = get_llm_config()
        # 客户端由 core.llm_router 统一持有：多供应商时每家一个连接池，
        # 引擎自己再存一个只会造成两份配置
        self.model = cfg["model"]
        self.temperature = cfg["temperature"]
        self.top_p = cfg["top_p"]
        self.frequency_penalty = cfg["frequency_penalty"]
        self.max_tokens = cfg["max_tokens"]

        self.slug = slug
        self.owner = owner
        self.ex_dir = resolve_ex_dir(slug, owner)
        self.vector_store = vector_store
        self.embedder = embedder
        self._rag_failures = 0
        self._rag_recovery_interval = 5  # 降级后每隔 N 轮尝试恢复一次
        self._turn_since_last_rag_attempt = 0

        self.skill_content = ""
        self.session_summaries: list[str] = []
        self.corrections = ""
        self.relationship_stage = "dating"  # 默认热恋期
        # 实际服务本次请求的供应商，供成本归集与观测
        self.last_provider = ""

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
            summary_files = sorted(sessions_dir.glob("*_summary.md"), reverse=True)[
                :RECENT_SESSIONS
            ]
            if summary_files:
                self.session_summaries = [
                    f.read_text(encoding="utf-8") for f in summary_files
                ]
            else:
                # 兼容旧归档（无摘要文件时直接读原始对话）
                raw_files = sorted(sessions_dir.glob("session_*.md"), reverse=True)[
                    :RECENT_SESSIONS
                ]
                self.session_summaries = [
                    f.read_text(encoding="utf-8") for f in raw_files
                ]

        corrections_path = self.ex_dir / "corrections.md"
        if corrections_path.exists():
            self.corrections = corrections_path.read_text(encoding="utf-8")

        # 关系阶段：Web 写 "stage"，CLI 写 "relationship_stage"，两个键都认。
        # 未知值回退默认阶段，注入 prompt 时才不会 KeyError
        meta_path = self.ex_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                stage = meta.get("stage") or meta.get("relationship_stage")
                if stage in STAGE_INSTRUCTIONS:
                    self.relationship_stage = stage
                elif stage:
                    logger.warning(
                        "未知关系阶段 %s，回退 %s", stage, self.relationship_stage
                    )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("加载 meta.json 失败: %s", e)

        logger.info("已连接 %s 的数字镜像 (model=%s)", self.slug, self.model)

    def _build_system_prompt(self, rag_results: Optional[list[dict]] = None) -> str:
        sticker_list = ", ".join(
            f"{sid}({s['label']})" for sid, s in IMAGE_STICKERS.items()
        )

        # 时间感知：让 AI 感知对话发生的时间背景
        now = datetime.now()
        time_context = (
            f"\n---\n## 时间感知\n"
            f"当前时间：{now.strftime('%Y年%m月%d日 %H:%M')}，"
            f"星期{['一', '二', '三', '四', '五', '六', '日'][now.weekday()]}。\n"
            f"请根据时间背景自然调整回复：\n"
            f"- 早上说早安、问吃早餐了吗\n"
            f"- 午饭时间问吃了什么\n"
            f"- 晚上问今天累不累\n"
            f"- 深夜（23点后）说还没睡呀、早点休息\n"
            f"- 周末可以提休息、出去玩\n"
            f"但不要每次都刻意提起时间，自然融入对话即可。\n"
        )

        # Token 预算：仅截断 session 摘要副本
        summaries = list(self.session_summaries)
        budget = int(LLM_MAX_CONTEXT_CHARS * 0.5)

        def _assemble(sums: list[str]) -> str:
            p = [self.skill_content]
            p.append(time_context)
            # 关系阶段决定整体语气基调（热恋/磨合/分手/治愈）
            stage_instruction = STAGE_INSTRUCTIONS.get(
                self.relationship_stage, STAGE_INSTRUCTIONS["dating"]
            )
            p.append(f"\n---\n## 当前关系阶段（语气基调）\n{stage_instruction}\n")
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
                    p.append(
                        "以下是从聊天记录中检索到的 ta 的原话，作为你回复的语气锚点：\n"
                    )
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
            summaries = summaries[:-1]  # 保留最新的，从最旧的开始删

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

    def _sampling_kwargs(self) -> dict:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "max_tokens": self.max_tokens,
        }

    def _call_api(self, messages: list[dict]):
        """经路由调用。重试负责单供应商的偶发失败，路由负责供应商级故障。

        每家重试 2 次而非 3 次：叠上故障转移后总尝试次数会翻倍，
        再按 3 次算延迟就太长了。
        """
        from core.llm_router import get_router

        @retry_api(max_attempts=2, base_delay=1.0)
        def invoke(client, model, **kwargs):
            return client.chat.completions.create(
                model=model, messages=messages, stream=False, **kwargs
            )

        response, provider = get_router().call(invoke, **self._sampling_kwargs())
        self.last_provider = provider.name
        return response

    def _prepare_messages(self, user_input: str, history: list[dict]) -> list[dict]:
        """构建完整的消息列表（system + history + user）。"""
        rag_results = self._rag_search(user_input)
        system_prompt = self._build_system_prompt(rag_results)
        history = self._fit_history_to_budget(system_prompt, user_input, history)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_input})
        return messages

    @staticmethod
    def _fit_history_to_budget(
        system_prompt: str, user_input: str, history: list[dict]
    ) -> list[dict]:
        """按总预算从最旧一端裁剪历史。

        system prompt 与本轮输入是不可裁的：前者是人格本身，后者是用户
        刚说的话。所以只能裁历史，且从旧到新——近处的上下文对连贯性更重要。
        超预算时报错并重试三次是最差的结果：必然失败，还消耗三倍配额。
        """
        cleaned = sanitize_chat_history(history)
        fixed_cost = estimate_tokens(system_prompt) + estimate_tokens(user_input)
        available = LLM_TOTAL_TOKEN_BUDGET - fixed_cost
        if available <= 0:
            # 人格本身就撑满了预算，只能放弃全部历史，让本轮至少能发出去
            logger.warning(
                "system prompt 已占满 token 预算（%d/%d），本轮不带历史",
                fixed_cost,
                LLM_TOTAL_TOKEN_BUDGET,
            )
            return []

        kept: list[dict] = []
        used = 0
        for message in reversed(cleaned):
            cost = estimate_tokens(message.get("content", ""))
            if used + cost > available:
                break
            kept.append(message)
            used += cost
        if len(kept) < len(cleaned):
            logger.info(
                "历史超出 token 预算，保留最近 %d/%d 条", len(kept), len(cleaned)
            )
        kept.reverse()
        return kept

    @staticmethod
    def _extract_sticker_tags(text: str) -> tuple[str, list[str]]:
        """从回复文本中提取 [sticker:xxx] 标记，返回 (清理后文本, 贴纸ID列表)。"""
        pattern = r"\[sticker:([a-zA-Z0-9_-]+)\]"
        sticker_ids = re.findall(pattern, text)
        clean_text = re.sub(pattern, "", text).strip()
        return clean_text, sticker_ids

    def chat(
        self, user_input: str, history: list[dict]
    ) -> tuple[str, list[str], object]:
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

    def _call_stream(self, messages):
        """流式同样经路由。"""
        from core.llm_router import get_router

        @retry_api(max_attempts=2, base_delay=1.0)
        def invoke(client, model, **kwargs):
            return client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                **kwargs,
            )

        stream, provider = get_router().call(invoke, **self._sampling_kwargs())
        self.last_provider = provider.name
        return stream

    def chat_stream(self, user_input: str, history: list[dict]):
        """流式对话，yield dict: {type: text|sticker|usage|red_packet, content/id: ...}"""
        messages = self._prepare_messages(user_input, history)

        full_reply = ""
        stream_usage = None
        stream = self._call_stream(messages)
        for chunk in stream:
            # include_usage 开启时，最后一个 chunk 只携带 usage、choices 为空
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                stream_usage = usage
            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            delta = choices[0].delta
            if delta.content:
                full_reply += delta.content
                yield {"type": "text", "content": delta.content}

        if stream_usage is None:
            # 兼容不支持 stream_options 的端点：回退估算必须覆盖完整请求体。
            # system prompt（SKILL.md 等）占单轮输入的大头，漏算会严重少计
            stream_usage = SimpleNamespace(
                prompt_tokens=estimate_tokens(
                    "\n".join(m["content"] for m in messages)
                ),
                completion_tokens=estimate_tokens(full_reply),
            )

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
            rp = create_redpacket(self.slug, trigger, owner=self.owner)
            if rp:
                yield {
                    "type": "red_packet",
                    "id": rp["id"],
                    "amount": rp["amount"],
                    "note": rp["note"],
                }

        # 计量口径与 /chat 一致：优先真实 usage，估算兜底同样计入 system prompt
        yield {
            "type": "usage",
            "prompt_tokens": getattr(stream_usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(stream_usage, "completion_tokens", 0) or 0,
        }
