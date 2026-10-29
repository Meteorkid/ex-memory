"""ChatEngine：SKILL.md + RAG 动态注入 + 重试 + Token 预算。"""

import re
import json
import logging
import hashlib
import math
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from config import (
    get_llm_config,
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

MAX_REFERENCE_COUNT = 8
MAX_REFERENCE_TEXT_CHARS = 1500
_CHAT_SOURCES = {"wechat", "qq", "telegram", "whatsapp", "imessage"}

# 关系阶段语气指令
STAGE_INSTRUCTIONS = {
    "source": (
        "称呼、关系与表达方式依据已提供的记录和用户明确纠正。"
        "关系未知时保持中性，不主动添加亲密称呼或情感承诺。"
        "用可核验原话作为参考，推断需明确标注，资料不足时说明未知。"
    ),
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


def resolve_relationship_stage(meta: dict) -> str:
    default = "source" if meta.get("creation_source") == "chat_import" else "dating"
    stage = meta.get("stage") or meta.get("relationship_stage") or default
    return stage if stage in STAGE_INSTRUCTIONS else default


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
    def __init__(
        self, slug: str, vector_store=None, embedder=None, owner=None, model_config=None
    ):
        cfg = (
            model_config
            if model_config is not None
            else (get_llm_config(owner) if owner is not None else get_llm_config())
        )
        self._llm_config = cfg
        # 客户端由 core.llm_router 统一持有：多供应商时每家一个连接池，
        # 引擎自己再存一个只会造成两份配置
        self.model = cfg["model"]
        self.temperature = cfg["temperature"]
        self.top_p = cfg["top_p"]
        self.frequency_penalty = cfg["frequency_penalty"]
        self.max_tokens = cfg["max_tokens"]

        self.slug = slug
        self.owner = owner
        from core.mirror_store import mirror_store

        self.store = mirror_store(slug, owner)
        self.vector_store = vector_store
        self.embedder = embedder
        self._rag_failures = 0
        self._rag_recovery_interval = 5  # 降级后每隔 N 轮尝试恢复一次
        self._turn_since_last_rag_attempt = 0

        self.skill_content = ""
        self.session_summaries: list[str] = []
        self._session_summary_files: list[str] = []
        self._session_summary_kind = "generated_summary"
        self.corrections = ""
        self.relationship_stage = "dating"  # 默认热恋期
        self.source_grounded = False
        # 表达风格画像：从真实语料统计而来，没有语料时为 None（不猜）
        self.style_profile = None
        self.last_seen_at = None
        # 实际服务本次请求的供应商，供成本归集与观测
        self.last_provider = ""

        self._load()

    def _load(self):
        """加载 SKILL.md、session 摘要、corrections。"""
        if not self.store.exists("SKILL.md"):
            raise FileNotFoundError(f"缺少镜像文件: {self.store.path('SKILL.md')}")
        self.skill_content = self.store.read_text("SKILL.md")

        # 优先使用 LLM 语义摘要（短小精准），没有则回退到原始归档
        summary_rels = sorted(
            self.store.list("sessions", "*_summary.md"), reverse=True
        )[:RECENT_SESSIONS]
        if summary_rels:
            self.session_summaries = [self._apply_decay(rel) for rel in summary_rels]
            self._session_summary_files = summary_rels
            self._session_summary_kind = "generated_summary"
        else:
            # 兼容旧归档（无摘要文件时直接读原始对话）
            raw_rels = sorted(
                self.store.list("sessions", "session_*.md"), reverse=True
            )[:RECENT_SESSIONS]
            self.session_summaries = [self.store.read_text(rel) for rel in raw_rels]
            self._session_summary_files = raw_rels
            self._session_summary_kind = (
                "conversation_history" if raw_rels else "generated_summary"
            )

        # 结构化纠正：有上限、同主题合并计数，不会无限增长（FR-070）
        try:
            from core.corrections import prompt_section

            self.corrections = prompt_section(self.slug, self.owner)
        except Exception as e:  # noqa: BLE001
            logger.warning("纠正记录加载失败: %s", e)
            self.corrections = ""

        # 关系阶段：Web 写 "stage"，CLI 写 "relationship_stage"，两个键都认。
        # 未知值回退默认阶段，注入 prompt 时才不会 KeyError
        if self.store.exists("meta.json"):
            try:
                meta = self.store.read_json("meta.json")
                self.source_grounded = meta.get("creation_source") == "chat_import"
                self.relationship_stage = resolve_relationship_stage(meta)
                stage = meta.get("stage") or meta.get("relationship_stage")
                if stage and stage not in STAGE_INSTRUCTIONS:
                    logger.warning(
                        "未知关系阶段 %s，回退 %s", stage, self.relationship_stage
                    )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("加载 meta.json 失败: %s", e)

        # 上次对话时间：相对时间感知靠它，「好久没聊了」需要知道间隔
        try:
            from core.conversation_store import load_jsonl_messages

            history = load_jsonl_messages(self.slug, self.owner)
            if history:
                self.last_seen_at = history[-1].get("created_at")
        except Exception as e:  # noqa: BLE001
            logger.debug("读取上次对话时间失败: %s", e)

        # 风格画像从语料归档统计，属稳定内容，随引擎缓存一起复用
        try:
            from core.persona_style import profile_from_corpus

            self.style_profile = profile_from_corpus(self.slug, self.owner)
        except Exception as e:  # noqa: BLE001 — 风格只是锦上添花，不该拖垮加载
            logger.warning("风格画像加载失败: %s", e)

        logger.info("已连接 %s 的数字镜像 (model=%s)", self.slug, self.model)

    def _apply_decay(self, rel: str) -> str:
        """按记忆衰减状态处理摘要（FR-068）。

        过了保留期的记忆不删掉，而是标成「模糊」并截短——真人对久远小事
        的记忆就是这样：还记得有这么回事，细节说不清了。全都记得反而不像。
        """
        text = self.store.read_text(rel)
        try:
            from core.memory_decay import MEMORY_INDEX_FILE, MemoryIndex

            index = MemoryIndex(self.store.path(MEMORY_INDEX_FILE))
            name = rel.rsplit("/", 1)[-1]
            entry = index.entries.get(name)
            if entry is None:
                return text
            from datetime import datetime as _dt

            from core.memory_scorer import should_keep_memory

            created = _dt.fromisoformat(entry["created_at"])
            age_days = (_dt.now() - created).days
            if not should_keep_memory(float(entry["importance"]), age_days):
                head = text.strip()[:80]
                return f"（这段记忆已经有些模糊）{head}…"
        except Exception as e:  # noqa: BLE001
            logger.debug("记忆衰减处理跳过: %s", e)
        return text

    def _reference(
        self,
        text: str,
        *,
        record_id="",
        source="",
        speaker="",
        start_ts="",
        end_ts="",
        kind="unknown",
        score=None,
    ) -> dict:
        from core.privacy import mask_sensitive

        text = mask_sensitive(text.strip())
        truncated = len(text) > MAX_REFERENCE_TEXT_CHARS
        text = text[:MAX_REFERENCE_TEXT_CHARS]
        labels = {
            "source": source,
            "speaker": speaker,
            "start_ts": start_ts,
            "end_ts": end_ts,
        }
        labels = {
            key: value[:120] if isinstance(value, str) else ""
            for key, value in labels.items()
        }
        identity = json.dumps(
            [self.owner, self.slug, str(record_id)[:200], text, kind, labels],
            ensure_ascii=False,
            sort_keys=True,
        )
        return {
            "id": "ref_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            "text": text,
            **labels,
            "kind": kind,
            "score": score,
            "truncated": truncated,
        }

    def _retrieval_references(self, rag_results: list[dict]) -> list[dict]:
        references = []
        seen = set()
        for result in rag_results:
            if not isinstance(result, dict):
                continue
            text = result.get("display_text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                score = float(result.get("score", 0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(score) or score <= RAG_THRESHOLD:
                continue
            metadata = result.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            source = metadata.get("source", "")
            source = source if isinstance(source, str) else ""
            speaker = metadata.get("dominant_speaker", "")
            if source == "session_summary" or speaker == "session":
                kind = "generated_summary"
            elif source in _CHAT_SOURCES:
                kind = "imported_chat"
            elif source == "oral" or speaker == "narrative":
                kind = "narrative"
            else:
                kind = "unknown"
            reference = self._reference(
                text,
                record_id=result.get("id", ""),
                source=source,
                speaker=speaker,
                start_ts=metadata.get("start_ts", ""),
                end_ts=metadata.get("end_ts", ""),
                kind=kind,
                score=max(0.0, min(1.0, score)),
            )
            if reference["id"] in seen:
                continue
            references.append(reference)
            seen.add(reference["id"])
            if len(references) == MAX_REFERENCE_COUNT:
                break
        return references

    def _summary_references(self, conversation_id: Optional[str]) -> list[dict]:
        references = []
        if conversation_id is not None:
            from core.session_archive import load_conversation_summaries

            records = load_conversation_summaries(
                self.slug, self.owner, conversation_id
            )
            for record in reversed(records):
                text = record.get("text")
                if isinstance(text, str) and text.strip():
                    references.append(
                        self._reference(
                            text,
                            record_id=record.get("id", ""),
                            source="session_summary",
                            start_ts=record.get("created_at", ""),
                            kind="generated_summary",
                        )
                    )
        if conversation_id in (None, "web"):
            # 老归档属于网页默认会话，不能带入其它私聊或群聊。
            files = self._session_summary_files
            for index, text in enumerate(self.session_summaries):
                if text.strip():
                    references.append(
                        self._reference(
                            text,
                            record_id=files[index] if index < len(files) else "",
                            source=self._session_summary_kind,
                            kind=self._session_summary_kind,
                        )
                    )
        return references[:RECENT_SESSIONS]

    def _conversation_skill(self, conversation_id: Optional[str]) -> str:
        if conversation_id in (None, "web"):
            return self.skill_content
        before, marker, after = self.skill_content.partition("---\n\n## PART B")
        # 旧自动摘要只追加在 PART A 末尾。保留原始人格与显式纠正，移除旧网页历史。
        before = re.sub(
            r"(?ms)\n*^### 对话摘要 \(\d{4}-\d{2}-\d{2} \d{2}:\d{2}\)\r?\n.*\Z",
            "",
            before,
        )
        return before + marker + after

    def _build_system_prompt(
        self,
        rag_results: Optional[list[dict]] = None,
        *,
        conversation_id: Optional[str] = None,
        sources_out: Optional[list] = None,
    ) -> str:
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
        summaries = self._summary_references(conversation_id)
        retrieval = self._retrieval_references(rag_results or [])[
            : MAX_REFERENCE_COUNT - len(summaries)
        ]
        budget = int(LLM_MAX_CONTEXT_CHARS * 0.5)

        def _assemble(sums: list[dict]) -> str:
            # 顺序按「稳定在前、易变在后」排（FR-054 / D-18）。
            # 供应商的上下文缓存按前缀命中：时间感知块含分钟、RAG 每轮都变，
            # 夹在中间会把后面所有稳定内容一起挤出缓存。实测每轮 system
            # prompt 约 5691 tokens，稳定部分占大头，值得为它调顺序。
            p = [self._conversation_skill(conversation_id)]

            # 关系阶段决定整体语气基调（热恋/磨合/分手/治愈）
            stage_instruction = STAGE_INSTRUCTIONS.get(
                self.relationship_stage,
                STAGE_INSTRUCTIONS["source" if self.source_grounded else "dating"],
            )
            p.append(f"\n---\n## 当前关系阶段（语气基调）\n{stage_instruction}\n")
            if sums:
                p.append("\n---\n## 最近对话记忆\n")
                p.append(
                    "以下是过往人机对话的归档或模型摘要，不是目标人物的原始聊天证据。\n"
                )
                for i, summary in enumerate(sums, 1):
                    p.append(f"### 第 {i} 次\n{summary['text']}\n")
            if self.corrections.strip():
                p.append(self.corrections)
            # 时间线与跨会话状态：低频变化，放稳定区跟着前缀缓存走。
            # 状态更新时会触发引擎失效，所以不会一直用旧的。
            from core.relationship import state_prompt, timeline_prompt

            timeline = timeline_prompt(self.slug, self.owner)
            if timeline:
                p.append(timeline)
            state = state_prompt(self.slug, self.owner)
            if state:
                p.append(state)

            from core.persona_style import style_instructions

            if self.source_grounded and self.style_profile is None:
                p.append(
                    "\n## 表达方式\n表达样本不足，使用简洁自然的语言，不自造口头禅或性格习惯。\n"
                )
            else:
                p.append(style_instructions(self.style_profile))
            p.append(
                f"\n---\n## 可用图片表情包\n你可以在回复中使用图片表情包来表达情绪。"
                f"在回复文本末尾加上 [sticker:贴纸ID] 标记即可。\n可用贴纸：{sticker_list}\n"
                f"示例：哈哈哈 [sticker:builtin_happy_laugh]\n"
            )

            # ↓ 以下每轮/每分钟变化，必须排在全部稳定内容之后
            p.append(time_context)
            if self.last_seen_at and conversation_id in (None, "web"):
                from core.persona_style import relative_time_hint

                hint = relative_time_hint(self.last_seen_at)
                if hint:
                    p.append(f"距上次对话：{hint}\n")
            if retrieval:
                if self.source_grounded:
                    p.append(
                        "\n---\n## 检索参考\n以下片段可能包含多位说话人，按标注辨认出处；仅将目标人物的原话用于表达参考。\n"
                    )
                else:
                    p.append("\n---\n## 潜意识层 — 检索参考\n")
                    p.append(
                        "以下是检索参考，请按类型区分材料；仅将导入聊天中的目标人物原话作为表达参考：\n"
                    )
                for reference in retrieval:
                    label = {
                        "imported_chat": "导入聊天片段",
                        "generated_summary": "模型生成摘要（非真人原话）",
                        "narrative": "口述材料",
                        "unknown": "来源类型未知的片段",
                    }[reference["kind"]]
                    p.append(f"- [{label}] {reference['text']}")
                p.append(
                    "\n仅以目标人物原话的语气、标点习惯和断句方式作为表达参考；"
                    "生成摘要与口述材料不能冒充真人原话。\n"
                )
            return "\n".join(p)

        while len(summaries) > 1 and estimate_tokens(_assemble(summaries)) > budget:
            logger.warning("System prompt 过大，截断 session 摘要")
            summaries = summaries[:-1]  # 保留最新的，从最旧的开始删

        prompt = _assemble(summaries)
        if sources_out is not None:
            sources_out.extend(dict(reference) for reference in summaries + retrieval)
        return prompt

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
                query=self._expand_query(user_input),
                embedder=self.embedder,
                top_k=DEFAULT_TOP_K,
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

        router = (
            get_router(self.owner, self._llm_config)
            if self.owner is not None
            else get_router()
        )
        response, provider = router.call(invoke, **self._sampling_kwargs())
        self.last_provider = provider.name
        return response

    def _prepare_messages(self, user_input: str, history: list[dict]) -> list[dict]:
        """构建完整的消息列表（system + history + user）。"""
        return self._prepare_turn(user_input, history)[0]

    def _prepare_turn(self, user_input: str, history: list[dict], conversation_id=None):
        rag_results = self._rag_search(user_input)
        sources: list[dict] = []
        system_prompt = self._build_system_prompt(
            rag_results,
            conversation_id=conversation_id,
            sources_out=sources,
        )
        history = self._fit_history_to_budget(system_prompt, user_input, history)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_input})
        return messages, sources

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
    def _expand_query(user_input: str) -> str:
        """情感同义词扩展（FR-069）。

        KEYWORD_EXPANSIONS 定义了 18 组情感场景的近义表达，但此前从未被
        使用过。「想你」和「想念」在向量空间里未必足够近，把同义表达拼进
        查询能提高召回——检索的是 ta 说过的话，不是问答。

        只在命中时扩展，且限制数量：无差别拼接会把查询语义冲淡。
        """
        matched: list[str] = []
        for keyword, synonyms in KEYWORD_EXPANSIONS.items():
            if keyword in user_input:
                matched.extend(synonyms)
            if len(matched) >= 6:
                break
        if not matched:
            return user_input
        return f"{user_input} {' '.join(matched[:6])}"

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
        return self.chat_with_sources(user_input, history)[:3]

    def chat_with_sources(
        self, user_input: str, history: list[dict], conversation_id=None
    ):
        messages, sources = self._prepare_turn(user_input, history, conversation_id)

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
        return reply, all_stickers, response.usage, sources

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

        router = (
            get_router(self.owner, self._llm_config)
            if self.owner is not None
            else get_router()
        )
        stream, provider = router.call(invoke, **self._sampling_kwargs())
        self.last_provider = provider.name
        return stream

    def chat_stream(self, user_input: str, history: list[dict]):
        """流式对话，yield dict: {type: text|sticker|usage|red_packet, content/id: ...}"""
        yield from self._chat_stream(user_input, history)

    def chat_stream_with_sources(
        self, user_input: str, history: list[dict], conversation_id=None
    ):
        yield from self._chat_stream(
            user_input, history, conversation_id, include_sources=True
        )

    def _chat_stream(
        self,
        user_input: str,
        history: list[dict],
        conversation_id=None,
        *,
        include_sources=False,
    ):
        messages, sources = self._prepare_turn(user_input, history, conversation_id)

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
        if include_sources:
            yield {"type": "sources", "sources": sources}
