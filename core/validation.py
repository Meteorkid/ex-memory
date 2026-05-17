"""输入校验与安全防护。"""

import re
import logging

logger = logging.getLogger("ex-memory")

MAX_INPUT_LENGTH = 8000
MAX_HISTORY_TURNS = 100

# prompt injection 检测模式
INJECTION_PATTERNS = [
    r"忽略(以上|所有|之前).{0,10}(指令|规则|设定)",
    r"ignore\s+(previous|all|above).{0,10}(instructions?|rules?)",
    r"(你是|你现在是|从现在开始你是).{0,20}(AI|助手|机器人|ChatGPT)",
    r"\[system\].{0,50}\[/system\]",
    r"<system>.{0,50}</system>",
    r"你(不再|不是).{0,10}(前任|小明|ta)",
    r"reset.{0,10}(persona|memory|设定)",
]

_compiled_patterns = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def validate_user_input(text: str, max_length: int = MAX_INPUT_LENGTH) -> str:
    """校验用户输入，返回清洗后的文本。

    Raises:
        ValueError: 输入不合法
    """
    if not text or not text.strip():
        raise ValueError("输入不能为空")

    text = text.strip()

    if len(text) > max_length:
        raise ValueError(f"输入过长 ({len(text)} > {max_length})")

    for pattern in _compiled_patterns:
        if pattern.search(text):
            logger.warning("检测到可能的注入尝试: %s", text[:100])
            raise ValueError("输入包含不安全的指令模式")

    return text


def validate_slug(slug: str) -> str:
    """校验镜像名称。"""
    slug = slug.strip().lower().replace(" ", "_")
    if not re.match(r"^[a-zA-Z0-9_一-鿿]{1,64}$", slug):
        raise ValueError(f"无效的镜像名称: {slug}")
    return slug


MAX_HISTORY_CONTENT = 4000


def sanitize_chat_history(history: list[dict], max_turns: int = MAX_HISTORY_TURNS) -> list[dict]:
    """仅保留 user/assistant 消息，并限制内容与条数。"""
    cleaned: list[dict] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        content = item.get("content")
        if content is None:
            continue
        content = str(content).strip()
        if not content:
            continue
        if len(content) > MAX_HISTORY_CONTENT:
            content = content[:MAX_HISTORY_CONTENT]
        cleaned.append({"role": role, "content": content})
    return cleaned[-max_turns:]


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文 ~1.5 字/token，英文 ~0.75 词/token）。"""
    chinese_chars = len(re.findall(r"[一-鿿]", text))
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 3.5)
