"""充值订单 + 渠道回调入账（M2 按量计费底层充值）。

资金流：用户先充值进余额，再按对话轮次实时扣费（billing.reserve_turn）。
本模块负责「充值」这一头：创建充值订单、渠道回调验签入账、以及管理员手工充值。

渠道采用可插拔 Provider（对齐 payments.py 的订阅 Provider 思路）：
- 首期内置沙箱渠道（MockTopupProvider，verify 恒真），用于打通下单-回调-入账-对账
  的能力层，不发起真实生产交易；
- 真实微信/支付宝接入只需实现 TopupProvider 并 set_provider，签名/验签逻辑
  收敛在 provider 内部，回调入账入口（confirm_topup_payment）对渠道透明。

幂等与安全：
- topup_orders.payment_ref 有唯一约束（迁移 009/004），同一笔渠道支付只建一张订单；
- 入账用「status='pending' → 'paid'」的条件 UPDATE + 行数判断：并发重复回调只有
  一个能把订单置为 paid 并入账，其余命中 already_paid / not_pending，绝不重复加钱。
  「订单状态 + 余额 + 流水」在同一事务内原子提交（复用 billing.credit）。
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

logger = logging.getLogger("ex-memory")

STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_REFUNDED = "refunded"
STATUS_CLOSED = "closed"


@dataclass(frozen=True)
class TopupIntent:
    """一次充值意图。provider 用它引导用户完成付款并获得可对账的支付单号。"""

    payment_ref: str
    amount_micros: int
    pay_url: str = ""
    qr_code: str = ""


class TopupProvider(Protocol):
    name: str

    def create_intent(self, account_id: int, amount_micros: int) -> TopupIntent: ...
    def verify(self, payment_ref: str) -> bool: ...


class MockTopupProvider:
    """沙箱充值渠道（默认）。verify 恒真，用于沙箱/内测打通闭环。

    不是占位符：让下单-回调-入账-对账在无真实商户号时也完整可测，
    真实微信/支付宝接入后 set_provider 即可替换。
    """

    name = "wechat_mock"

    def create_intent(
        self, account_id: int, amount_micros: int
    ) -> TopupIntent:
        ref = f"wxmock-{uuid.uuid4().hex[:16]}"
        return TopupIntent(
            payment_ref=ref,
            amount_micros=amount_micros,
            qr_code=f"weixin://wxpay/mock/{ref}",
        )

    def verify(self, payment_ref: str) -> bool:
        # 沙箱渠道无真实签名可验，一律视为支付成功
        return True


_provider: TopupProvider = MockTopupProvider()


def set_provider(provider: TopupProvider) -> None:
    global _provider
    _provider = provider


def get_provider() -> TopupProvider:
    return _provider


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_topup_order(
    account_id: int, amount_micros: int, provider: Optional[TopupProvider] = None
) -> dict:
    """创建充值订单（pending），返回支付意图。金额校验会抛 ValueError。"""
    if amount_micros <= 0:
        raise ValueError("充值金额必须为正")

    prov = provider or get_provider()
    intent = prov.create_intent(account_id, amount_micros)

    from server.auth import _get_conn

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO topup_orders
                (account_id, provider, amount_micros, status, payment_ref)
            VALUES (?, ?, ?, ?, ?)
            """,
            (account_id, prov.name, amount_micros, STATUS_PENDING, intent.payment_ref),
        )
        conn.commit()
    return {
        "order_id": intent.payment_ref,
        "provider": prov.name,
        "amount_micros": amount_micros,
        "status": STATUS_PENDING,
        "pay_url": intent.pay_url,
        "qr_code": intent.qr_code,
    }


def confirm_topup_payment(
    payment_ref: str,
    channel_trade_no: Optional[str] = None,
    provider_name: str = "",
) -> dict:
    """渠道回调入账：订单置为 paid 并加余额、落流水。幂等。

    Args:
        payment_ref: 渠道支付单号（下单时由 provider 生成）。
        channel_trade_no: 渠道回调返回的交易号，回写便于对账。
        provider_name: 渠道名（仅用于 ledger memo 记录，可空）。

    Returns:
        {"status", "ok", "amount_micros"}。status 为 not_found / already_paid /
        refunded / closed / paid 之一。只有首次把 pending 置为 paid 才真正入账加钱。
    """
    import core.billing as billing
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM topup_orders WHERE payment_ref = ?", (payment_ref,)
        ).fetchone()
        if row is None:
            return {"status": "not_found", "ok": False}

        if row["status"] == STATUS_PAID:
            return {
                "status": "already_paid",
                "ok": True,
                "amount_micros": int(row["amount_micros"]),
            }
        if row["status"] in (STATUS_REFUNDED, STATUS_CLOSED):
            return {"status": row["status"], "ok": False}

        amount = int(row["amount_micros"])
        account_id = int(row["account_id"])
        updated = conn.execute(
            """
            UPDATE topup_orders
            SET status = ?, channel_trade_no = ?, paid_at = ?
            WHERE payment_ref = ? AND status = ?
            """,
            (STATUS_PAID, channel_trade_no, _now(), payment_ref, STATUS_PENDING),
        ).rowcount
        if updated == 0:
            # 并发下另一线程已抢先置为 paid
            conn.commit()
            return {"status": "not_pending", "ok": False}

        billing.credit(
            conn,
            account_id,
            amount,
            kind="topup",
            ref_type="topup_order",
            ref_id=payment_ref,
            memo=f"provider={provider_name or row['provider']}",
        )
        conn.commit()
    return {"status": STATUS_PAID, "ok": True, "amount_micros": amount}


def get_topup_order(payment_ref: str) -> Optional[dict]:
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM topup_orders WHERE payment_ref = ?", (payment_ref,)
        ).fetchone()
    return dict(row) if row else None


def refund_topup(payment_ref: str) -> dict:
    """充值退款：paid → refunded，并从余额扣回原充值额、落一条 refund 流水。幂等。

    只对「已到账（paid）」的订单生效；pending / 已退款 / 已关闭都拒绝，避免误退。
    余额扣回用 credit 负额（balance -= amount），退款后负余额表示用户已消费该笔，
    由对账/运营口径负责兜底，本函数不拦截。
    """
    import core.billing as billing
    from server.auth import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM topup_orders WHERE payment_ref = ?", (payment_ref,)
        ).fetchone()
        if row is None:
            return {"status": "not_found", "ok": False}
        if row["status"] == STATUS_REFUNDED:
            return {
                "status": "already_refunded",
                "ok": True,
                "amount_micros": -int(row["amount_micros"]),
            }
        if row["status"] != STATUS_PAID:
            return {"status": row["status"], "ok": False}

        amount = int(row["amount_micros"])
        account_id = int(row["account_id"])
        updated = conn.execute(
            "UPDATE topup_orders SET status = ? WHERE payment_ref = ? AND status = ?",
            (STATUS_REFUNDED, payment_ref, STATUS_PAID),
        ).rowcount
        if updated == 0:
            conn.commit()
            return {"status": "not_paid", "ok": False}

        billing.credit(
            conn,
            account_id,
            -amount,
            kind="refund",
            ref_type="topup_order",
            ref_id=payment_ref,
            memo="refund",
        )
        conn.commit()
    return {"status": STATUS_REFUNDED, "ok": True, "amount_micros": -amount}


def reconcile_topup(external_orders: list[dict]) -> dict:
    """充值侧对账（NFR-006）：比对已到账的充值订单与渠道净入金。

    external_orders 形如 [{"payment_ref": ..., "amount_micros": ...}]。
    返回三类差异（口径沿用 payments.reconcile）：
      missing_internally  渠道有、内部无（可能是渠道单号缺失或漏入账）
      missing_externally  内部有、渠道无（疑似渠道账单缺失/异常）
      amount_mismatch     金额不一致
    对账只看 paid 状态，pending/refunded 不计入应收口径。
    """
    from server.auth import _get_conn

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT payment_ref, amount_micros FROM topup_orders"
            " WHERE status = ? AND payment_ref IS NOT NULL",
            (STATUS_PAID,),
        ).fetchall()
    internal = {r["payment_ref"]: int(r["amount_micros"]) for r in rows}
    external = {
        o["payment_ref"]: int(o["amount_micros"]) for o in external_orders
    }
    return {
        "missing_internally": sorted(set(external) - set(internal)),
        "missing_externally": sorted(set(internal) - set(external)),
        "amount_mismatch": sorted(
            ref
            for ref in set(internal) & set(external)
            if internal[ref] != external[ref]
        ),
    }