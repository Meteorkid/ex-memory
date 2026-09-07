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


class BalanceInsufficient(Exception):
    """余额不足，无法支付一轮对话。按量计费下用于拦截（对应旧配额制 QuotaExceeded）。"""

    def __init__(self, account_id: int, balance_micros: int, turn_price_micros: int):
        super().__init__(
            f"余额不足（{balance_micros / 1_000_000:.2f} 元），本轮单价 "
            f"{turn_price_micros / 1_000_000:.2f} 元，请先充值。"
        )
        self.account_id = account_id
        self.balance_micros = balance_micros
        self.turn_price_micros = turn_price_micros


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


# ── 余额账本（按量计费）──


def _turn_price() -> int:
    from config import TURN_PRICE_MICROS

    return int(TURN_PRICE_MICROS)


def _ensure_balance_row(conn, account_id: int) -> None:
    """惰性建余额行。新账号首次建行时写入初始体验余额，避免一分钱没有。"""
    from config import INITIAL_BALANCE_MICROS

    row = conn.execute(
        "SELECT balance_micros FROM account_balances WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is not None:
        return
    conn.execute(
        "INSERT INTO account_balances (account_id, balance_micros, updated_at)"
        " VALUES (?, ?, ?)",
        (account_id, int(INITIAL_BALANCE_MICROS), _now()),
    )


def _add_ledger(
    conn,
    account_id: int,
    kind: str,
    amount_micros: int,
    *,
    turn_price: Optional[int] = None,
    ref_type: str = "",
    ref_id: str = "",
    memo: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO ledger (account_id, kind, amount_micros, turn_price_micros,
                            ref_type, ref_id, memo)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (account_id, kind, amount_micros, turn_price, ref_type, ref_id, memo),
    )


def balance_status(account_id: int) -> dict:
    """余额视图：余额、单价、可支撑轮数。用户侧余额查询走这里。"""
    from server.auth import _get_conn

    with _get_conn() as conn:
        _ensure_balance_row(conn, account_id)
        row = conn.execute(
            "SELECT balance_micros FROM account_balances WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        conn.commit()
    balance = int(row["balance_micros"])
    price = _turn_price()
    return {
        "account_id": account_id,
        "balance_micros": balance,
        "turn_price_micros": price,
        "affordable_turns": balance // price if price else 0,
    }


def credit(
    conn,
    account_id: int,
    amount_micros: int,
    *,
    kind: str,
    turn_price: Optional[int] = None,
    ref_type: str = "",
    ref_id: str = "",
    memo: str = "",
) -> int:
    """事务内入账：加余额并落一条 ledger 流水，返回入账后余额。

    与 `topup()` 的区别：不自己开事务，把余额与流水写入放进**调用方的事务**，
    用于充值回调这类「订单状态 + 余额 + 流水」必须原子一起提交的场景。
    由调用方负责 commit。
    """
    _ensure_balance_row(conn, account_id)
    conn.execute(
        "UPDATE account_balances SET balance_micros = balance_micros + ?,"
        " updated_at = ? WHERE account_id = ?",
        (amount_micros, _now(), account_id),
    )
    _add_ledger(
        conn,
        account_id,
        kind,
        amount_micros,
        turn_price=turn_price,
        ref_type=ref_type,
        ref_id=ref_id,
        memo=memo,
    )
    row = conn.execute(
        "SELECT balance_micros FROM account_balances WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return int(row["balance_micros"])


def topup(
    account_id: int,
    amount_micros: int,
    *,
    provider: str = "manual",
    payment_ref: str = "",
    ref_id: str = "",
) -> int:
    """充值入账：金额加进余额并落一条 topup 流水。返回入账后余额。

    管理员手工充值（无渠道订单）走这里的完整事务。幂等交给上层
    （topup_orders.payment_ref 唯一约束），本函数只做「入账」这一动作。
    """
    from server.auth import _get_conn

    if amount_micros <= 0:
        raise ValueError("充值金额必须为正")
    with _get_conn() as conn:
        _ensure_balance_row(conn, account_id)
        conn.execute(
            "UPDATE account_balances SET balance_micros = balance_micros + ?,"
            " updated_at = ? WHERE account_id = ?",
            (amount_micros, _now(), account_id),
        )
        _add_ledger(
            conn,
            account_id,
            "topup",
            amount_micros,
            ref_type="topup_order" if ref_id else provider,
            ref_id=ref_id or payment_ref,
            memo=f"provider={provider}",
        )
        row = conn.execute(
            "SELECT balance_micros FROM account_balances WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        conn.commit()
    return int(row["balance_micros"])


# ── 配额 ──


def _turn_limit(conn, account_id: int) -> int:
    """同一事务连接内读取套餐月轮次（仅作统计，放行由余额决定）。"""
    row = conn.execute(
        "SELECT plan FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    name = row["plan"] if row else PLAN_FREE
    return PLANS.get(name, PLANS[PLAN_FREE]).monthly_turns


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
    """预扣一轮费用。返回 ALLOW，余额不足以支付本轮时抛 BalanceInsufficient。

    按量计费：放行与否由**余额**是否 ≥ 本轮单价决定，用原子 UPDATE 预扣，
    并发下也不会超扣。配额表的 turns_reserved 仅作轮次统计（历史维度），
    不再作为放行门槛。
    """
    from server.auth import _get_conn

    price = _turn_price()
    period = current_period()
    with _get_conn() as conn:
        _ensure_balance_row(conn, account_id)
        cur = conn.execute(
            "UPDATE account_balances SET balance_micros = balance_micros - ?,"
            " updated_at = ? WHERE account_id = ? AND balance_micros >= ?",
            (price, _now(), account_id, price),
        )
        if cur.rowcount == 0:
            row = conn.execute(
                "SELECT balance_micros FROM account_balances WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            balance = int(row["balance_micros"]) if row else 0
            conn.commit()
            raise BalanceInsufficient(account_id, balance, price)

        # 轮次统计：保留 turns_reserved 计数，放行不依赖 quota.limit
        _ensure_quota_row(conn, account_id, period, _turn_limit(conn, account_id))
        conn.execute(
            "UPDATE quotas SET turns_reserved = turns_reserved + 1, updated_at = ?"
            " WHERE account_id = ? AND period = ?",
            (_now(), account_id, period),
        )
        conn.commit()
    return ALLOW


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
        # 用户侧应收：每轮固定单价（与 cost 即供应商成本分离）。
        # 余额已在 reserve_turn 预扣，此处只落不可变流水，供对账核对应收总额。
        price = _turn_price()
        _add_ledger(
            conn,
            account_id,
            "consume",
            -price,
            turn_price=price,
            ref_type="turn",
            ref_id=str(account_id),
        )
        conn.commit()
    return cost


def release_reservation(account_id: int) -> None:
    """调用失败时释放预扣：撤销配额计数，并把 reserve 预扣的余额加回。

    没有这一步就是「扣了钱没生成」——用户为一次失败的请求付了费。
    """
    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute(
            "UPDATE quotas SET turns_reserved = CASE WHEN turns_reserved > 0"
            " THEN turns_reserved - 1 ELSE 0 END, updated_at = ?"
            " WHERE account_id = ? AND period = ?",
            (_now(), account_id, current_period()),
        )
        conn.execute(
            "UPDATE account_balances SET balance_micros = balance_micros + ?,"
            " updated_at = ? WHERE account_id = ?",
            (_turn_price(), _now(), account_id),
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
