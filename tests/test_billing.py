"""计量、配额、两阶段扣减与订阅（FR-050 ~ FR-056）。

计量单位是**对话轮次**而非字数：实测每轮固定成本约 5700 输入 tokens，
与用户输入长短基本无关。
"""

import hashlib
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    import server.auth as auth
    import server.routes as routes_mod

    db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", db)
    monkeypatch.setattr(auth, "DB_DIR", db.parent)
    auth.init_db()
    monkeypatch.setattr("config.EXES_DIR", tmp_path / "exes")
    noop = MagicMock()
    noop.check = MagicMock()
    monkeypatch.setattr(routes_mod, "_login_limiter", noop)
    monkeypatch.setattr(routes_mod, "_check_exe_access", lambda slug, uid: slug)
    return tmp_path


@pytest.fixture
def client(env):
    from server.app import create_app

    return TestClient(create_app())


def _account(client, request, suffix="", admin=False):
    name = "bil_" + hashlib.md5((request.node.name + suffix).encode()).hexdigest()[:12]
    client.post("/api/auth/register", json={"username": name, "password": "test123456"})
    if admin:
        from server.auth import set_user_role

        set_user_role(name, "admin")
    token = client.post(
        "/api/auth/login", json={"username": name, "password": "test123456"}
    ).json()["token"]
    from server.auth import validate_token

    return {"Authorization": f"Bearer {token}"}, validate_token(token)


class TestAccountProvisioning:
    def test_account_created_lazily_and_reused(self, client, request):
        from core.billing import ensure_account

        _headers, user_id = _account(client, request)
        first = ensure_account(user_id)
        assert ensure_account(user_id) == first

    def test_unknown_user_raises(self, env):
        from core.billing import ensure_account

        with pytest.raises(LookupError):
            ensure_account(999999)

    def test_default_plan_is_free(self, client, request):
        from core.billing import PLAN_FREE, ensure_account, get_plan

        _headers, user_id = _account(client, request)
        assert get_plan(ensure_account(user_id)).name == PLAN_FREE


class TestTwoPhaseDeduction:
    """只做事后扣会超发，只做事前扣会「扣了没生成」，两者都得有。"""

    def test_reserve_then_settle_counts_once(self, client, request):
        from core.billing import ensure_account, quota_status, reserve_turn, settle_turn

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)

        reserve_turn(account_id)
        assert quota_status(account_id)["turns_used"] == 1
        settle_turn(account_id, prompt_tokens=5000, completion_tokens=100)
        # 预扣转结算，总数仍是 1
        assert quota_status(account_id)["turns_used"] == 1

    def test_release_undoes_reservation(self, client, request):
        """调用失败必须释放，否则用户为一次失败请求付了钱。"""
        from core.billing import (
            ensure_account,
            quota_status,
            release_reservation,
            reserve_turn,
        )

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        reserve_turn(account_id)
        release_reservation(account_id)
        assert quota_status(account_id)["turns_used"] == 0

    def test_reservations_do_not_oversell_balance(self, client, request):
        """并发下不能超扣：余额只够 N 轮时最多放行 N 轮，第 N+1 轮抛余额不足。"""
        from core.billing import (
            BalanceInsufficient,
            balance_status,
            ensure_account,
            reserve_turn,
        )

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        affordable = balance_status(account_id)["affordable_turns"]

        granted = 0
        for _ in range(affordable + 5):
            try:
                reserve_turn(account_id)
                granted += 1
            except BalanceInsufficient:
                break
        assert granted == affordable
        assert balance_status(account_id)["balance_micros"] == 0

    def test_release_never_goes_negative(self, client, request):
        from core.billing import ensure_account, quota_status, release_reservation

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        release_reservation(account_id)
        release_reservation(account_id)
        assert quota_status(account_id)["turns_used"] == 0


class TestBalancePolicy:
    """按量计费：放行只由余额决定，套餐不影响（无月度额度上限，也无降级模型）。"""

    def test_insufficient_balance_blocks_reserve(self, client, request):
        from core.billing import (
            ALLOW,
            BalanceInsufficient,
            balance_status,
            ensure_account,
            reserve_turn,
            topup,
        )
        from config import INITIAL_BALANCE_MICROS

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        # 掏空初始余额
        affordable = balance_status(account_id)["affordable_turns"]
        for _ in range(affordable):
            reserve_turn(account_id)
        with pytest.raises(BalanceInsufficient):
            reserve_turn(account_id)
        # 充值后恢复可继续
        topup(account_id, INITIAL_BALANCE_MICROS)
        assert reserve_turn(account_id) == ALLOW

    def test_topup_credits_balance_and_ledger(self, client, request):
        from core.billing import balance_status, ensure_account, topup

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        before = balance_status(account_id)["balance_micros"]
        new_bal = topup(account_id, 100_000, ref_id="order-1")
        assert new_bal == before + 100_000

        from server.auth import _get_conn

        with _get_conn() as conn:
            row = conn.execute(
                "SELECT kind, amount_micros, ref_id FROM ledger"
                " WHERE account_id = ? ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
        assert row["kind"] == "topup" and int(row["amount_micros"]) == 100_000
        assert row["ref_id"] == "order-1"

    def test_settle_writes_consume_ledger(self, client, request):
        from core.billing import ensure_account, settle_turn
        from config import TURN_PRICE_MICROS
        from server.auth import _get_conn

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        settle_turn(account_id, prompt_tokens=100, completion_tokens=10)

        with _get_conn() as conn:
            row = conn.execute(
                "SELECT kind, amount_micros, turn_price_micros FROM ledger"
                " WHERE account_id = ? AND kind = 'consume' ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
        assert int(row["amount_micros"]) == -int(TURN_PRICE_MICROS)
        assert int(row["turn_price_micros"]) == int(TURN_PRICE_MICROS)


class TestTopupOrder:
    """充值订单 + 回调入账（core.topup）：下单、幂等入账、并发防重复、404/订单状态保护。"""

    def test_create_order_then_callback_credits(self, client, request):
        from core.billing import balance_status, ensure_account
        from core.topup import confirm_topup_payment, create_topup_order

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        before = balance_status(account_id)["balance_micros"]

        order = create_topup_order(account_id, 500_000)
        assert order["status"] == "pending" and order["amount_micros"] == 500_000

        result = confirm_topup_payment(order["order_id"], channel_trade_no="wx-1")
        assert result["status"] == "paid" and result["ok"] is True
        assert balance_status(account_id)["balance_micros"] == before + 500_000

    def test_callback_idempotent_no_double_credit(self, client, request):
        from core.billing import balance_status, ensure_account
        from core.topup import confirm_topup_payment, create_topup_order

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        before = balance_status(account_id)["balance_micros"]

        order = create_topup_order(account_id, 200_000)
        paid = confirm_topup_payment(order["order_id"])
        assert paid["status"] == "paid"
        again = confirm_topup_payment(order["order_id"])
        assert again["status"] == "already_paid" and again["ok"] is True
        assert balance_status(account_id)["balance_micros"] == before + 200_000

    def test_callback_unknown_order(self, client, request):
        from core.topup import confirm_topup_payment

        result = confirm_topup_payment("nope-nope-nope-nope")
        assert result["status"] == "not_found" and result["ok"] is False

    def test_topup_http_endpoints(self, client, request):
        """POST /account/topup 建单 + POST /payment/callback 入账 走完整闭环。"""
        headers, user_id = _account(client, request)

        resp = client.post(
            "/api/account/topup", headers=headers, json={"amount_micros": 300_000}
        )
        assert resp.status_code == 200
        order = resp.json()
        assert order["status"] == "pending"

        cb = client.post(
            "/api/payment/callback",
            json={"payment_ref": order["order_id"], "channel_trade_no": "wx-web"},
        )
        assert cb.status_code == 200
        # 重复回调不报错且不再加钱
        cb2 = client.post(
            "/api/payment/callback", json={"payment_ref": order["order_id"]}
        )
        assert cb2.status_code == 200

        bal = client.get(
            "/api/account/balance", headers=headers
        ).json()
        assert bal["balance_micros"] >= 300_000

    def test_callback_unknown_http_404(self, client, request):
        resp = client.post(
            "/api/payment/callback", json={"payment_ref": "no-such-order-ref"}
        )
        assert resp.status_code == 404


class TestTopupRefund:
    """充值退款 + 充值侧对账（NFR-006）：幂等退款、余额扣回、三类差异。"""

    def test_refund_credits_back_balance_once(self, client, request):
        from core.billing import balance_status, ensure_account
        from core.topup import (
            confirm_topup_payment,
            create_topup_order,
            refund_topup,
        )

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        before = balance_status(account_id)["balance_micros"]

        order = create_topup_order(account_id, 400_000)
        confirm_topup_payment(order["order_id"])
        assert balance_status(account_id)["balance_micros"] == before + 400_000

        refund = refund_topup(order["order_id"])
        assert refund["status"] == "refunded" and refund["ok"] is True
        assert balance_status(account_id)["balance_micros"] == before

        # 幂等：重复退款不重复扣
        again = refund_topup(order["order_id"])
        assert again["status"] == "already_refunded" and again["ok"] is True
        assert balance_status(account_id)["balance_micros"] == before

    def test_refund_rejects_unpaid_order(self, client, request):
        from core.billing import ensure_account
        from core.topup import create_topup_order, refund_topup

        _headers, user_id = _account(client, request)
        order = create_topup_order(ensure_account(user_id), 100_000)
        result = refund_topup(order["order_id"])
        assert result["status"] == "pending" and result["ok"] is False

    def test_reconcile_three_kinds_of_discrepancy(self, client, request):
        from core.billing import ensure_account
        from core.topup import (
            confirm_topup_payment,
            create_topup_order,
            reconcile_topup,
        )

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)

        a = create_topup_order(account_id, 100_000)
        confirm_topup_payment(a["order_id"])  # 内部+渠道都有的正常单
        b = create_topup_order(account_id, 200_000)
        confirm_topup_payment(b["order_id"])

        report = reconcile_topup(
            [
                {"payment_ref": a["order_id"], "amount_micros": 100_000},
                {"payment_ref": "channel-only-ref", "amount_micros": 50_000},
            ]
        )
        # b 只在内部 → missing_externally；channel-only-ref 只在渠道 → missing_internally
        assert b["order_id"] in report["missing_externally"]
        assert "channel-only-ref" in report["missing_internally"]

    def test_reconcile_amount_mismatch(self, client, request):
        from core.billing import ensure_account
        from core.topup import (
            confirm_topup_payment,
            create_topup_order,
            reconcile_topup,
        )

        _headers, user_id = _account(client, request)
        order = create_topup_order(ensure_account(user_id), 300_000)
        confirm_topup_payment(order["order_id"])

        report = reconcile_topup(
            [{"payment_ref": order["order_id"], "amount_micros": 999}]
        )
        assert order["order_id"] in report["amount_mismatch"]


class TestCostAccounting:
    def test_cached_tokens_are_cheaper(self):
        from core.billing import estimate_cost_micros

        full = estimate_cost_micros("m", 10000, 100, cached_tokens=0)
        cached = estimate_cost_micros("m", 10000, 100, cached_tokens=8000)
        assert cached < full, "缓存命中没有体现为更低的成本"

    def test_cost_uses_integer_math(self):
        """整数运算避免浮点累加误差。"""
        from core.billing import estimate_cost_micros

        assert isinstance(estimate_cost_micros("m", 1234, 567), int)

    def test_pricing_override_from_config(self, monkeypatch):
        from core.billing import estimate_cost_micros

        monkeypatch.setattr(
            "config.LLM_PRICING",
            {"pricey": {"prompt_per_1k": 10000, "completion_per_1k": 10000}},
        )
        assert estimate_cost_micros("pricey", 1000, 0) == 10000

    def test_usage_record_written_on_settle(self, client, request):
        from core.billing import ensure_account, settle_turn
        from server.auth import _get_conn

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        settle_turn(
            account_id,
            user_id=user_id,
            slug="s",
            provider="p",
            model="m",
            prompt_tokens=5691,
            completion_tokens=42,
        )
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM usage_records WHERE account_id = ?", (account_id,)
            ).fetchone()
        assert int(row["prompt_tokens"]) == 5691
        assert int(row["cost_micros"]) > 0


class TestChatIntegration:
    def test_insufficient_balance_blocks_chat_before_engine(self, client, request):
        from core.billing import balance_status, ensure_account, reserve_turn

        headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        for _ in range(balance_status(account_id)["affordable_turns"]):
            reserve_turn(account_id)

        with patch("server.routes._get_engine") as get_engine:
            resp = client.post(
                "/api/chat", json={"slug": "demo", "message": "在吗"}, headers=headers
            )
        assert resp.json()["notice"]["type"] == "insufficient_balance"
        get_engine.assert_not_called()

    def test_successful_chat_settles_usage(self, client, request):
        from core.billing import ensure_account, quota_status

        headers, user_id = _account(client, request)
        engine = MagicMock()
        engine.chat.return_value = (
            "你好",
            [],
            MagicMock(prompt_tokens=5000, completion_tokens=20),
        )
        engine.last_provider = "deepseek"
        engine.model = "deepseek-chat"

        with patch("server.routes._get_engine", return_value=engine):
            with patch("server.routes._run_session_archive"):
                client.post(
                    "/api/chat",
                    json={"slug": "demo", "message": "在吗"},
                    headers=headers,
                )

        status = quota_status(ensure_account(user_id))
        assert status["turns_used"] == 1
        assert status["cost_micros"] > 0

    def test_engine_failure_releases_reservation(self, client, request):
        """调用失败必须释放预扣。"""
        from core.billing import ensure_account, quota_status

        headers, user_id = _account(client, request)
        engine = MagicMock()
        engine.chat.side_effect = RuntimeError("LLM 挂了")

        with patch("server.routes._get_engine", return_value=engine):
            client.post(
                "/api/chat", json={"slug": "demo", "message": "在吗"}, headers=headers
            )
        assert quota_status(ensure_account(user_id))["turns_used"] == 0

    def test_blocked_content_does_not_consume_quota(self, client, request):
        """被内容审核拦下的请求根本没调 LLM，不该扣额度。"""
        from core.billing import ensure_account, quota_status

        headers, user_id = _account(client, request)
        with patch("server.routes._get_engine"):
            client.post(
                "/api/chat",
                json={"slug": "demo", "message": "有人代开发票吗"},
                headers=headers,
            )
        assert quota_status(ensure_account(user_id))["turns_used"] == 0


class TestSubscription:
    def test_free_plan_cannot_be_subscribed(self, client, request):
        headers, _ = _account(client, request)
        resp = client.post(
            "/api/account/subscribe", json={"plan": "free"}, headers=headers
        )
        assert resp.status_code == 400

    def test_subscribe_creates_pending_order(self, client, request):
        headers, _ = _account(client, request)
        body = client.post(
            "/api/account/subscribe", json={"plan": "standard"}, headers=headers
        ).json()
        assert body["status"] == "pending" and body["order_id"]

    def test_activation_upgrades_plan_and_is_idempotent(self, client, request):
        from core.billing import ensure_account, get_plan
        from core.payments import activate_subscription

        headers, user_id = _account(client, request)
        order = client.post(
            "/api/account/subscribe", json={"plan": "standard"}, headers=headers
        ).json()

        assert activate_subscription(order["order_id"]) is True
        assert get_plan(ensure_account(user_id)).name == "standard"
        # 幂等：重复确认不再延长
        assert activate_subscription(order["order_id"]) is True

    def test_refund_downgrades_to_free(self, client, request):
        from core.billing import ensure_account, get_plan
        from core.payments import activate_subscription, refund_subscription

        headers, user_id = _account(client, request)
        order = client.post(
            "/api/account/subscribe", json={"plan": "premium"}, headers=headers
        ).json()
        activate_subscription(order["order_id"])
        assert refund_subscription(order["order_id"]) is True
        assert get_plan(ensure_account(user_id)).name == "free"

    def test_expiry_downgrades_to_free(self, client, request):
        from core.billing import ensure_account, get_plan
        from core.payments import activate_subscription, expire_overdue

        headers, user_id = _account(client, request)
        order = client.post(
            "/api/account/subscribe", json={"plan": "standard"}, headers=headers
        ).json()
        activate_subscription(order["order_id"], days=-1)
        assert expire_overdue() == 1
        assert get_plan(ensure_account(user_id)).name == "free"


class TestReconciliation:
    def test_detects_all_three_discrepancy_kinds(self, client, request):
        from core.payments import reconcile

        headers, _ = _account(client, request)
        order = client.post(
            "/api/account/subscribe", json={"plan": "standard"}, headers=headers
        ).json()

        result = reconcile(
            [
                {"payment_ref": order["order_id"], "amount_micros": 1},  # 金额不符
                {"payment_ref": "只在外部有的单", "amount_micros": 100},
            ]
        )
        assert result["amount_mismatch"] == [order["order_id"]]
        assert result["missing_internally"] == ["只在外部有的单"]
        assert result["missing_externally"] == []


class TestMarginDashboard:
    def test_admin_sees_loss_making_accounts(self, client, request):
        from core.billing import ensure_account, set_plan, settle_turn

        admin_headers, _admin_id = _account(client, request, "adm", admin=True)
        _user_headers, user_id = _account(client, request, "usr")
        account_id = ensure_account(user_id)
        set_plan(account_id, "standard")
        # 制造一个成本远超订阅价（39 元）的账号：
        # 每轮 100 万 prompt tokens 约 20 万 micros，200 轮 ≈ 56 元
        for _ in range(200):
            settle_turn(account_id, prompt_tokens=1_000_000, completion_tokens=100_000)

        body = client.get("/api/admin/billing/margins", headers=admin_headers).json()
        assert any(a["account_id"] == account_id for a in body["loss_making"])

    def test_margins_require_admin(self, client, request):
        headers, _ = _account(client, request)
        assert (
            client.get("/api/admin/billing/margins", headers=headers).status_code == 403
        )

    def test_user_usage_endpoint_hides_margin(self, client, request):
        """毛利是运营视角，不该出现在用户侧。"""
        headers, _ = _account(client, request)
        body = client.get("/api/account/usage", headers=headers).json()
        assert "margin_micros" not in body and "revenue_micros" not in body
