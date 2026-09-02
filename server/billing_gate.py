"""配额闸门：预扣、结算、释放。

放在内容审核之后、引擎之前。顺序不能换：被危机干预或内容审核拦下的请求
根本没调 LLM，不该扣用户额度。
"""

import logging
from typing import Optional

logger = logging.getLogger("ex-memory")


class QuotaBlocked(Exception):
    """配额耗尽，携带面向用户的提示。"""

    def __init__(self, notice: dict):
        super().__init__(notice.get("message", "配额已用完"))
        self.notice = notice


def reserve(user_id: int) -> tuple[Optional[int], str]:
    """预扣一轮，返回 (account_id, 结果)。配额耗尽时抛 QuotaBlocked。

    计费主体解析失败时**降级为不计量**而非阻断请求：计量出问题不该让用户
    连话都说不了。account_id 为 None 时后续 settle/release 自动跳过。
    这条路径会打错误日志——收不上钱是要有人知道的。
    """
    from core.billing import ALLOW, QuotaExceeded, ensure_account, reserve_turn

    try:
        account_id = ensure_account(user_id)
    except Exception as e:  # noqa: BLE001
        logger.error("计费主体解析失败 user=%s，本轮不计量: %s", user_id, e)
        return None, ALLOW

    try:
        outcome = reserve_turn(account_id)
    except QuotaExceeded as e:
        raise QuotaBlocked(
            {
                "type": "quota_exceeded",
                "message": str(e),
            }
        ) from e
    if outcome != ALLOW:
        logger.info("账号 %s 超额，本轮降级到低成本模型", account_id)
    return account_id, outcome


def settle(
    account_id: Optional[int],
    usage,
    *,
    user_id: Optional[int] = None,
    slug: Optional[str] = None,
    provider: str = "",
    model: str = "",
    kind: str = "chat",
) -> None:
    """结算。usage 为 None 时按 0 token 结算——轮次照扣，因为确实调用了。"""
    if account_id is None:
        return

    from core.billing import settle_turn

    prompt = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    completion = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    cached = int(getattr(usage, "cached_tokens", 0) or 0) if usage else 0
    try:
        settle_turn(
            account_id,
            user_id=user_id,
            slug=slug,
            kind=kind,
            provider=provider,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
        )
    except Exception as e:  # noqa: BLE001 — 计量失败不该让用户拿不到回复
        logger.error("计量结算失败 account=%s: %s", account_id, e)


def release(account_id: Optional[int]) -> None:
    """调用失败时释放预扣，否则就是「扣了额度没生成」。"""
    if account_id is None:
        return

    from core.billing import release_reservation

    try:
        release_reservation(account_id)
    except Exception as e:  # noqa: BLE001
        logger.error("释放预扣失败 account=%s: %s", account_id, e)
