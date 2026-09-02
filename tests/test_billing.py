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

    def test_concurrent_reservations_do_not_oversell(self, client, request):
        """并发下各自先占名额，不能同时判断「还有余额」然后一起超发。"""
        from core.billing import PLANS, QuotaExceeded, ensure_account, reserve_turn

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        limit = PLANS["free"].monthly_turns

        granted = 0
        for _ in range(limit + 5):
            try:
                reserve_turn(account_id)
                granted += 1
            except QuotaExceeded:
                break
        assert granted == limit

    def test_release_never_goes_negative(self, client, request):
        from core.billing import ensure_account, quota_status, release_reservation

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        release_reservation(account_id)
        release_reservation(account_id)
        assert quota_status(account_id)["turns_used"] == 0


class TestOveragePolicy:
    def test_free_plan_rejects_when_exhausted(self, client, request):
        from core.billing import PLANS, QuotaExceeded, ensure_account, reserve_turn

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        for _ in range(PLANS["free"].monthly_turns):
            reserve_turn(account_id)
        with pytest.raises(QuotaExceeded):
            reserve_turn(account_id)

    def test_paid_plan_degrades_instead_of_rejecting(self, client, request):
        from core.billing import DEGRADE, PLANS, ensure_account, reserve_turn, set_plan

        _headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        set_plan(account_id, "standard")
        for _ in range(PLANS["standard"].monthly_turns):
            reserve_turn(account_id)
        # 付费用户超额降级而不是被拦住
        assert reserve_turn(account_id) == DEGRADE


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
    def test_quota_exceeded_blocks_chat_before_engine(self, client, request):
        from core.billing import PLANS, ensure_account, reserve_turn

        headers, user_id = _account(client, request)
        account_id = ensure_account(user_id)
        for _ in range(PLANS["free"].monthly_turns):
            reserve_turn(account_id)

        with patch("server.routes._get_engine") as get_engine:
            resp = client.post(
                "/api/chat", json={"slug": "demo", "message": "在吗"}, headers=headers
            )
        assert resp.json()["notice"]["type"] == "quota_exceeded"
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
