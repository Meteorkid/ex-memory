"""支付与订阅（FR-055）。

**只定义接口，不绑任何支付 SDK。**真实接入微信支付/支付宝需要商户号、
API 密钥与回调证书，那是商务与运维的事；仓库里硬编码某一家的 SDK 只会
在选型变化时变成负担。

内置 ManualProvider：管理员手工开通。它不是占位符——小规模运营、内测、
补偿性开通都用得上，而且让订阅链路在没有支付通道时也是完整可测的。

对账（NFR-006）靠 payment_ref 的唯一约束：同一笔外部订单不会被重复入账。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

logger = logging.getLogger("ex-memory")


@dataclass(frozen=True)
class PaymentIntent:
    """一次支付意图。provider 用它引导用户完成付款。"""

    order_id: str
    amount_micros: int
    plan: str
    pay_url: str = ""
    qr_code: str = ""


class PaymentProvider(Protocol):
    name: str

    def create_intent(
        self, account_id: int, plan: str, amount_micros: int
    ) -> PaymentIntent: ...
    def verify(self, order_id: str) -> bool: ...


class ManualProvider:
    """管理员手工开通。不是占位符——内测与补偿性开通都用得上。"""

    name = "manual"

    def create_intent(
        self, account_id: int, plan: str, amount_micros: int
    ) -> PaymentIntent:
        import uuid

        return PaymentIntent(
            order_id=f"manual-{uuid.uuid4().hex[:16]}",
            amount_micros=amount_micros,
            plan=plan,
        )

    def verify(self, order_id: str) -> bool:
        # 手工开通没有外部状态可查，由管理员调用 activate 直接生效
        return False


_provider: PaymentProvider = ManualProvider()


def set_provider(provider: PaymentProvider) -> None:
    global _provider
    _provider = provider


def get_provider() -> PaymentProvider:
    return _provider


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_subscription(account_id: int, plan: str) -> dict:
    """创建待支付订阅，返回支付意图。"""
    from core.billing import PLANS

    if plan not in PLANS:
        raise ValueError(f"未知套餐: {plan}")
    plan_def = PLANS[plan]
    if plan_def.price_micros <= 0:
        raise ValueError("免费套餐无需订阅")

    intent = _provider.create_intent(account_id, plan, plan_def.price_micros)

    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions
                (account_id, plan, status, provider, payment_ref, amount_micros)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                plan,
                "pending",
                _provider.name,
                intent.order_id,
                intent.amount_micros,
            ),
        )
        conn.commit()
    return {
        "order_id": intent.order_id,
        "plan": plan,
        "amount_micros": intent.amount_micros,
        "pay_url": intent.pay_url,
        "qr_code": intent.qr_code,
        "status": "pending",
    }


def activate_subscription(payment_ref: str, days: int = 30) -> bool:
    """确认到账并激活。幂等：重复调用不会延长两次。

    payment_ref 有唯一约束，同一笔外部订单不会被重复入账——这是对账
    （NFR-006）的基础。
    """
    from core.billing import set_plan
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE payment_ref = ?", (payment_ref,)
        ).fetchone()
        if row is None:
            return False
        if row["status"] == "active":
            return True  # 幂等：已激活就不再延长

        started = datetime.now(timezone.utc)
        expires = started + timedelta(days=days)
        conn.execute(
            """
            UPDATE subscriptions
            SET status = 'active', started_at = ?, expires_at = ?
            WHERE payment_ref = ?
            """,
            (started.isoformat(), expires.isoformat(), payment_ref),
        )
        conn.commit()
        account_id = int(row["account_id"])
        plan = row["plan"]

    set_plan(account_id, plan)
    logger.info("订阅已激活 account=%s plan=%s ref=%s", account_id, plan, payment_ref)
    return True


def refund_subscription(payment_ref: str) -> bool:
    """退款：订阅置为 refunded 并把账号降回免费套餐。"""
    from core.billing import PLAN_FREE, set_plan
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscriptions WHERE payment_ref = ?", (payment_ref,)
        ).fetchone()
        if row is None or row["status"] == "refunded":
            return False
        conn.execute(
            "UPDATE subscriptions SET status = 'refunded' WHERE payment_ref = ?",
            (payment_ref,),
        )
        conn.commit()
        account_id = int(row["account_id"])

    set_plan(account_id, PLAN_FREE)
    logger.info("订阅已退款 account=%s ref=%s", account_id, payment_ref)
    return True


def list_subscriptions(account_id: int) -> list[dict]:
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT plan, status, provider, payment_ref, amount_micros,
                   started_at, expires_at, created_at
            FROM subscriptions WHERE account_id = ? ORDER BY created_at DESC
            """,
            (account_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def expire_overdue() -> int:
    """把过期订阅降回免费套餐。定时任务调用，返回处理数量。"""
    from core.billing import PLAN_FREE, set_plan
    from server.auth import _get_conn

    now = _now()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT account_id, payment_ref FROM subscriptions"
            " WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE subscriptions SET status = 'expired' WHERE payment_ref = ?",
                (row["payment_ref"],),
            )
        conn.commit()

    for row in rows:
        set_plan(int(row["account_id"]), PLAN_FREE)
    if rows:
        logger.info("处理了 %d 个过期订阅", len(rows))
    return len(rows)


def reconcile(external_orders: list[dict]) -> dict:
    """与供应商账单对账（NFR-006）。

    external_orders 形如 [{"payment_ref": ..., "amount_micros": ...}]。
    返回三类差异：只在外部有、只在内部有、金额不一致。
    """
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT payment_ref, amount_micros, status FROM subscriptions"
            " WHERE payment_ref IS NOT NULL"
        ).fetchall()
    internal = {r["payment_ref"]: int(r["amount_micros"]) for r in rows}
    external = {o["payment_ref"]: int(o["amount_micros"]) for o in external_orders}

    return {
        "missing_internally": sorted(set(external) - set(internal)),
        "missing_externally": sorted(set(internal) - set(external)),
        "amount_mismatch": sorted(
            ref
            for ref in set(internal) & set(external)
            if internal[ref] != external[ref]
        ),
    }
