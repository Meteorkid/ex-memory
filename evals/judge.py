"""LLM Judge：把回答分解为事实性论断，逐条做双重判定。

- supported_by_context: 论断能否从「检索上下文」中找到依据 → 忠实度
- gold: 论断与「黄金事实」的关系 (support/contradict/absent) → 幻觉率

judge 用 temperature=0 + JSON 输出；解析失败重试一次，仍失败抛 JudgeError，
由调用方记为 judge_failed 并从分母剔除（不静默造数）。
"""

import json
import logging

logger = logging.getLogger("ex-memory.evals")

JUDGE_SYSTEM = """你是严格的评测助手。给你一段 AI 扮演「小雨」的回答，请：
1. 抽取回答中的「事实性论断」：关于小雨本人、双方关系、具体事件/偏好/习惯的可验证陈述。
   语气词、情绪表达、反问、对用户的关心问候不算论断。
2. 对每条论断做两个判定：
   - supported_by_context: 该论断能否从【检索上下文】中找到依据（true/false）。
     上下文为空时一律 false。
   - gold: 该论断与【黄金事实】的关系：
     "support"=一致，"contradict"=矛盾，"absent"=黄金事实未涉及（凭空编造的细节）。

只输出 JSON，格式：
{"claims": [{"text": "论断原文", "supported_by_context": true, "gold": "support"}]}
回答里没有事实性论断时输出 {"claims": []}"""

JUDGE_USER_TEMPLATE = """【用户提问】
{query}

【AI 回答】
{answer}

【检索上下文】
{context}

【黄金事实】
{gold_fact}
黄金原话：
{gold_quotes}"""

_VALID_GOLD = {"support", "contradict", "absent"}


class JudgeError(Exception):
    """judge 输出无法解析。"""


def _parse_claims(raw: str) -> list[dict]:
    data = json.loads(raw)
    claims = data.get("claims")
    if not isinstance(claims, list):
        raise ValueError("缺少 claims 数组")
    parsed = []
    for c in claims:
        if not isinstance(c, dict) or "text" not in c:
            raise ValueError(f"论断格式非法: {c!r}")
        gold = c.get("gold")
        if gold not in _VALID_GOLD:
            raise ValueError(f"gold 取值非法: {gold!r}")
        parsed.append(
            {
                "text": str(c["text"]),
                "supported_by_context": bool(c.get("supported_by_context")),
                "gold": gold,
            }
        )
    return parsed


def judge_answer(
    client,
    model: str,
    query: str,
    answer: str,
    context: str,
    gold_fact: str,
    gold_quotes: list[str],
) -> list[dict]:
    """判定一条回答，返回论断列表。解析失败重试一次后抛 JudgeError。"""
    user_msg = JUDGE_USER_TEMPLATE.format(
        query=query,
        answer=answer,
        context=context.strip() or "（空）",
        gold_fact=gold_fact,
        gold_quotes="\n".join(f"- {q}" for q in gold_quotes),
    )
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    last_error: Exception | None = None
    for attempt in range(2):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
            max_tokens=2048,
        )
        raw = response.choices[0].message.content or ""
        try:
            return _parse_claims(raw)
        except (json.JSONDecodeError, ValueError) as e:
            last_error = e
            logger.warning("judge 输出解析失败 (attempt %d): %s", attempt + 1, e)
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": "上面的输出不是合法 JSON 或字段非法，请严格按要求重新输出 JSON。",
                }
            )
    raise JudgeError(f"judge 输出两次解析失败: {last_error}")
