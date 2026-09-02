"""计量、配额与套餐（FR-050 ~ FR-053）。

**计量单位是对话轮次，不是字数。**实测每轮固定成本约 5700 输入 tokens
（SKILL.md 人格档案占大头），与用户输入长短基本无关。按字数计费既不反映
真实成本，也会让用户为「在吗」这种短消息困惑于到底扣了多少。

**两阶段扣减**：调 LLM 前先预扣一次额度，拿到真实用量后再结算。
只做「事后扣」会出现「生成了没扣」（并发下超发），只做「事前扣」会出现
「扣了没生成」（调用失败）。两者都得有。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("ex-memory")

PLAN_FREE = "free"
PLAN_STANDARD = "standard"
PLAN_PREMIUM = "premium"


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_turns: int
    max_mirrors: int
    # 超额策略：reject 直接拒绝，degrade 降级到低成本模型
    overage: str
    price_micros: int


PLANS: dict[str, Plan] = {
    PLAN_FREE: Plan(
        PLAN_FREE, monthly_turns=100, max_mirrors=1, overage="reject", price_micros=0
    ),
    PLAN_STANDARD: Plan(
        PLAN_STANDARD,
        monthly_turns=2000,
        max_mirrors=3,
        overage="degrade",
        price_micros=39_000_000,
    ),
    PLAN_PREMIUM: Plan(
        PLAN_PREMIUM,
        monthly_turns=10000,
        max_mirrors=10,
        overage="degrade",
        price_micros=99_000_000,
    ),
}

# 超额判定结果
ALLOW = "allow"
DEGRADE = "degrade"
REJECT = "reject"


class QuotaExceeded(Exception):
    """配额耗尽且套餐策略为拒绝。"""


def current_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 计费主体 ──


def ensure_account(user_id: int) -> int:
    """确保用户有计费主体，返回 account_id。

    个人用户就是 1 人一个 account。这一层现在看是多余的，但没有它，
    将来做团队版就得改动所有业务表的外键。
    """
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT account_id FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"用户 {user_id} 不存在")
        if row["account_id"] is not None:
            return int(row["account_id"])

        account_id = int(
            conn.insert_returning_id(
                "INSERT INTO accounts (plan, status) VALUES (?, ?)",
                (PLAN_FREE, "active"),
            )
        )
        conn.execute(
            "UPDATE users SET account_id = ? WHERE id = ?", (account_id, user_id)
        )
        conn.commit()
        return account_id


def get_plan(account_id: int) -> Plan:
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT plan FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    name = row["plan"] if row else PLAN_FREE
    return PLANS.get(name, PLANS[PLAN_FREE])


def set_plan(account_id: int, plan: str) -> None:
    if plan not in PLANS:
        raise ValueError(f"未知套餐: {plan}")
    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute("UPDATE accounts SET plan = ? WHERE id = ?", (plan, account_id))
        conn.commit()


# ── 配额 ──


def _ensure_quota_row(conn, account_id: int, period: str, limit: int) -> dict:
    row = conn.execute(
        "SELECT * FROM quotas WHERE account_id = ? AND period = ?",
        (account_id, period),
    ).fetchone()
    if row is not None:
        return dict(row)
    conn.execute(
        "INSERT INTO quotas (account_id, period, turns_limit) VALUES (?, ?, ?)",
        (account_id, period, limit),
    )
    return {
        "account_id": account_id,
        "period": period,
        "turns_limit": limit,
        "turns_reserved": 0,
        "turns_settled": 0,
        "cost_micros": 0,
    }


def quota_status(account_id: int) -> dict:
    from server.auth import _get_conn

    plan = get_plan(account_id)
    period = current_period()
    with _get_conn() as conn:
        row = _ensure_quota_row(conn, account_id, period, plan.monthly_turns)
        conn.commit()
    used = int(row["turns_reserved"]) + int(row["turns_settled"])
    return {
        "plan": plan.name,
        "period": period,
        "turns_limit": int(row["turns_limit"]),
        "turns_used": used,
        "turns_remaining": max(0, int(row["turns_limit"]) - used),
        "cost_micros": int(row["cost_micros"]),
        "overage_policy": plan.overage,
    }


def reserve_turn(account_id: int) -> str:
    """预扣一轮额度。返回 ALLOW / DEGRADE，或抛 QuotaExceeded。

    预扣发生在调 LLM 之前：并发请求各自先占住名额，避免同时判断「还有余额」
    然后一起超发。
    """
    from server.auth import _get_conn

    plan = get_plan(account_id)
    period = current_period()
    with _get_conn() as conn:
        row = _ensure_quota_row(conn, account_id, period, plan.monthly_turns)
        used = int(row["turns_reserved"]) + int(row["turns_settled"])
        limit = int(row["turns_limit"])

        if used >= limit:
            if plan.overage == "reject":
                conn.commit()
                raise QuotaExceeded(
                    f"本月对话额度已用完（{limit} 轮）。升级套餐或等下个周期重置。"
                )
            outcome = DEGRADE
        else:
            outcome = ALLOW

        conn.execute(
            "UPDATE quotas SET turns_reserved = turns_reserved + 1, updated_at = ?"
            " WHERE account_id = ? AND period = ?",
            (_now(), account_id, period),
        )
        conn.commit()
    return outcome


def settle_turn(
    account_id: int,
    *,
    user_id: Optional[int] = None,
    slug: Optional[str] = None,
    kind: str = "chat",
    provider: str = "",
    model: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
) -> int:
    """结算一轮：预扣转为已结算，并落一条计量记录。返回成本（micros）。"""
    from server.auth import _get_conn

    cost = estimate_cost_micros(model, prompt_tokens, completion_tokens, cached_tokens)
    period = current_period()
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE quotas
            SET turns_reserved = CASE WHEN turns_reserved > 0
                                      THEN turns_reserved - 1 ELSE 0 END,
                turns_settled = turns_settled + 1,
                cost_micros = cost_micros + ?,
                updated_at = ?
            WHERE account_id = ? AND period = ?
            """,
            (cost, _now(), account_id, period),
        )
        conn.execute(
            """
            INSERT INTO usage_records
                (account_id, user_id, slug, kind, provider, model,
                 prompt_tokens, completion_tokens, cached_tokens, cost_micros)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                user_id,
                slug,
                kind,
                provider,
                model,
                prompt_tokens,
                completion_tokens,
                cached_tokens,
                cost,
            ),
        )
        conn.commit()
    return cost


def release_reservation(account_id: int) -> None:
    """调用失败时释放预扣。

    没有这一步就是「扣了额度没生成」——用户为一次失败的请求付了钱。
    """
    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute(
            "UPDATE quotas SET turns_reserved = CASE WHEN turns_reserved > 0"
            " THEN turns_reserved - 1 ELSE 0 END, updated_at = ?"
            " WHERE account_id = ? AND period = ?",
            (_now(), account_id, current_period()),
        )
        conn.commit()


# ── 成本 ──

# 单价：百万分之一元 / 1000 tokens。缺省值按 deepseek-chat 量级设，
# 真实单价随供应商与合同变动，应由运维按 LLM_PRICING 覆盖。
DEFAULT_PRICING = {
    "prompt_per_1k": 200,
    "completion_per_1k": 800,
    "cached_per_1k": 20,
}


def _pricing_for(model: str) -> dict:
    import config

    table = config.LLM_PRICING or {}
    return table.get(model, table.get("default", DEFAULT_PRICING))


def estimate_cost_micros(
    model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0
) -> int:
    """按 token 数估算成本。整数运算，避免浮点累加误差。"""
    pricing = _pricing_for(model)
    billable_prompt = max(0, prompt_tokens - cached_tokens)
    return (
        billable_prompt * pricing["prompt_per_1k"] // 1000
        + completion_tokens * pricing["completion_per_1k"] // 1000
        + cached_tokens * pricing.get("cached_per_1k", 0) // 1000
    )


def account_cost_summary(account_id: int, period: Optional[str] = None) -> dict:
    """单账号的成本汇总，用于识别亏损用户（FR-056）。"""
    from server.auth import _get_conn

    period = period or current_period()
    plan = get_plan(account_id)
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS turns,
                   COALESCE(SUM(cost_micros), 0) AS cost,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM usage_records
            WHERE account_id = ? AND created_at LIKE ?
            """,
            (account_id, f"{period}%"),
        ).fetchone()

    cost = int(row["cost"])
    revenue = plan.price_micros
    return {
        "account_id": account_id,
        "period": period,
        "plan": plan.name,
        "turns": int(row["turns"]),
        "prompt_tokens": int(row["prompt_tokens"]),
        "completion_tokens": int(row["completion_tokens"]),
        "cost_micros": cost,
        "revenue_micros": revenue,
        "margin_micros": revenue - cost,
        "is_loss_making": revenue > 0 and cost > revenue,
    }
