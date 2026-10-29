"""主动消息生成计费、输出审核和最终投递约束。"""

from datetime import datetime
from types import SimpleNamespace

import pytest


@pytest.fixture
def proactive_env(tmp_path, monkeypatch):
    import server.auth as auth
    from core import proactive
    from core.mirror_store import mirror_store
    from mcp_server import service

    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "DB_DIR", tmp_path)
    auth.init_db()
    user_id = auth.get_or_create_external_user_id("test", "proactive-owner")
    slug = "guardmirror"
    mirror_store(slug, user_id).write_json("meta.json", {"owner_user_id": user_id})
    proactive.set_config(slug, owner=user_id, enabled=True, quiet_start=0, quiet_end=0)
    monkeypatch.setattr(proactive, "health_blocks", lambda _uid: None)
    service.invalidate_engine()
    yield user_id, slug
    service.invalidate_engine()


@pytest.fixture
def generation(proactive_env, monkeypatch):
    from core import proactive
    from mcp_server import service

    events = []
    usage = SimpleNamespace(prompt_tokens=13, completion_tokens=7)
    state = {"reply": "今天也记得好好休息", "usage": usage, "error": None}

    class Engine:
        model = "test-model"
        last_provider = "test-provider"

        def chat(self, message, history):
            events.append("generate")
            if state["error"]:
                raise state["error"]
            return state["reply"], [], state["usage"]

    def reserve(_uid):
        events.append("reserve")
        return 31, "allow"

    def check_output(_uid, _slug, _text):
        events.append("review")
        return None

    def settle(account_id, used, **kwargs):
        events.append("settle")
        state["settled"] = (account_id, used, kwargs)

    original_queue = proactive.queue_message

    def queue(*args, **kwargs):
        events.append("queue")
        return original_queue(*args, **kwargs)

    monkeypatch.setattr(service, "_get_engine", lambda *_args: Engine())
    monkeypatch.setattr(proactive, "decide_trigger", lambda *a, **k: "morning")
    monkeypatch.setattr("server.billing_gate.reserve", reserve)
    monkeypatch.setattr("server.billing_gate.settle", settle)
    monkeypatch.setattr(
        "server.billing_gate.release", lambda _id: events.append("release")
    )
    monkeypatch.setattr("server.safety_gate.check_output", check_output)
    monkeypatch.setattr(proactive, "queue_message", queue)
    return events, state


def test_generated_message_is_reviewed_queued_and_charged(proactive_env, generation):
    from mcp_server import service

    events, state = generation
    user_id, slug = proactive_env
    result = service.proactive_tick(user_id, slug)
    assert result["sent"] is True
    assert events == ["reserve", "generate", "review", "queue", "settle"]
    account_id, usage, metadata = state["settled"]
    assert account_id == 31
    assert usage is state["usage"]
    assert metadata == {
        "user_id": user_id,
        "slug": slug,
        "provider": "test-provider",
        "model": "test-model",
        "kind": "proactive",
    }


def test_exhausted_quota_does_not_generate(proactive_env, generation, monkeypatch):
    from mcp_server import service
    from server.billing_gate import QuotaBlocked

    def exhausted(_uid):
        raise QuotaBlocked({"type": "quota_exceeded", "message": "额度已用完"})

    monkeypatch.setattr("server.billing_gate.reserve", exhausted)
    result = service.proactive_tick(*proactive_env)
    assert result["sent"] is False
    assert result["content"] == ""
    assert result["notice"]["type"] == "quota_exceeded"
    assert generation[0] == []


def test_blocked_output_is_not_queued_and_reservation_is_released(
    proactive_env, generation, monkeypatch
):
    from core.proactive import pending_messages
    from mcp_server import service

    monkeypatch.setattr(
        "server.safety_gate.check_output",
        lambda *a: {"type": "blocked", "message": "内容被拦截"},
    )
    result = service.proactive_tick(*proactive_env)
    assert result["sent"] is False
    assert result["content"] == ""
    assert generation[0] == ["reserve", "generate", "release"]
    assert pending_messages(*proactive_env) == []


def test_empty_generation_releases_reservation(proactive_env, generation):
    from mcp_server import service

    generation[1]["reply"] = "  "
    result = service.proactive_tick(*proactive_env)
    assert result["sent"] is False
    assert generation[0] == ["reserve", "generate", "release"]


def test_generation_error_releases_reservation(proactive_env, generation):
    from mcp_server import service

    generation[1]["error"] = RuntimeError("synthetic generation failure")
    with pytest.raises(service.ServiceError, match="无法生成"):
        service.proactive_tick(*proactive_env)
    assert generation[0] == ["reserve", "generate", "release"]


def test_queue_error_releases_reservation(proactive_env, generation, monkeypatch):
    from mcp_server import service

    def queue_failure(*args, **kwargs):
        raise OSError("synthetic queue failure")

    monkeypatch.setattr("core.proactive.queue_message", queue_failure)
    with pytest.raises(service.ServiceError):
        service.proactive_tick(*proactive_env)
    assert generation[0] == ["reserve", "generate", "review", "release"]


def test_no_trigger_does_not_reserve(proactive_env, generation, monkeypatch):
    from mcp_server import service

    monkeypatch.setattr("core.proactive.decide_trigger", lambda *a, **k: None)
    assert service.proactive_tick(*proactive_env)["sent"] is False
    assert generation[0] == []


def test_compose_preserves_usage_and_legacy_string_api():
    from core.proactive import compose, compose_with_usage

    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2)
    engine = SimpleNamespace(chat=lambda *a: ("  早上好  ", [], usage))
    assert compose_with_usage(engine, "morning") == ("早上好", usage)
    assert compose(engine, "morning") == "早上好"


@pytest.mark.parametrize("hour", [0, 7, 23])
def test_pending_is_not_deliverable_during_quiet_hours(
    proactive_env, monkeypatch, hour
):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    proactive.set_config(slug, owner=user_id, quiet_start=23, quiet_end=8)
    message_id = proactive.queue_message(slug, user_id, "morning", "早", owner=user_id)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 29, hour, 0)

    monkeypatch.setattr(proactive, "datetime", Clock)
    result = service.proactive_prepare_delivery(user_id, slug, message_id)
    assert result["deliverable"] is False
    assert result["content"] == ""
    assert "免打扰" in result["reason"]
    assert len(proactive.pending_messages(user_id, slug)) == 1
    assert (
        "免打扰" in service.proactive_status(user_id, slug)["delivery_blocked_reason"]
    )


def test_pending_delivery_checks_current_output_policy(proactive_env, monkeypatch):
    from core import proactive
    from mcp_server import service
    from server.auth import _get_conn

    user_id, slug = proactive_env
    message_id = proactive.queue_message(
        slug, user_id, "manual", "旧队列内容", owner=user_id
    )
    reviewed = []

    def reject(uid, mirror, content):
        reviewed.append((uid, mirror, content))
        return {"type": "blocked", "message": "审核拦截"}

    monkeypatch.setattr("server.safety_gate.check_output", reject)
    result = service.proactive_prepare_delivery(user_id, slug, message_id)
    assert result["deliverable"] is False
    assert result["content"] == ""
    assert reviewed == [(user_id, slug, "旧队列内容")]
    assert proactive.pending_messages(user_id, slug) == []
    with _get_conn() as conn:
        stored = conn.execute(
            "SELECT status, content, delivered_at FROM proactive_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    assert dict(stored) == {
        "status": "dismissed",
        "content": "旧队列内容",
        "delivered_at": None,
    }


def test_rejected_pending_does_not_block_later_safe_generation(
    proactive_env, generation, monkeypatch
):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    message_id = proactive.queue_message(
        slug, user_id, "manual", "拒绝标记", owner=user_id
    )
    monkeypatch.setattr(
        "server.safety_gate.check_output",
        lambda uid, mirror, content: (
            {"type": "blocked", "message": "审核拦截"}
            if content == "拒绝标记"
            else None
        ),
    )
    assert (
        service.proactive_prepare_delivery(user_id, slug, message_id)["deliverable"]
        is False
    )
    assert service.proactive_status(user_id, slug)["pending_count"] == 0
    generated = service.proactive_tick(user_id, slug)
    assert generated["sent"] is True
    assert (
        service.proactive_prepare_delivery(user_id, slug, generated["message_id"])[
            "deliverable"
        ]
        is True
    )


def test_empty_legacy_pending_is_dismissed(proactive_env):
    from core import proactive
    from mcp_server import service
    from server.auth import _get_conn

    user_id, slug = proactive_env
    message_id = proactive.queue_message(
        slug, user_id, "manual", "旧消息", owner=user_id
    )
    with _get_conn() as conn:
        conn.execute(
            "UPDATE proactive_messages SET content = '  ' WHERE id = ?", (message_id,)
        )
        conn.commit()
    result = service.proactive_prepare_delivery(user_id, slug, message_id)
    assert result["deliverable"] is False
    assert proactive.pending_messages(user_id, slug) == []
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT status, delivered_at FROM proactive_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    assert dict(row) == {"status": "dismissed", "delivered_at": None}


def test_dismiss_is_scoped_and_only_changes_pending(proactive_env):
    from core import proactive
    from server.auth import _get_conn

    user_id, slug = proactive_env
    message_id = proactive.queue_message(
        slug, user_id, "manual", "保留证据", owner=user_id
    )
    assert proactive.dismiss_message(message_id, user_id + 1, slug) == 0
    assert proactive.dismiss_message(message_id, user_id, "othermirror") == 0
    assert len(proactive.pending_messages(user_id, slug)) == 1
    assert proactive.dismiss_message(message_id, user_id, slug) == 1
    assert proactive.dismiss_message(message_id, user_id, slug) == 0

    delivered_id = proactive.queue_message(
        slug, user_id, "manual", "已送达", owner=user_id
    )
    proactive.mark_delivered([delivered_id], user_id)
    assert proactive.dismiss_message(delivered_id, user_id, slug) == 0
    with _get_conn() as conn:
        delivered = conn.execute(
            "SELECT status, delivered_at FROM proactive_messages WHERE id = ?",
            (delivered_id,),
        ).fetchone()
    assert delivered["status"] == "delivered"
    assert delivered["delivered_at"] is not None


def test_delivery_rechecks_restrictions_after_review(proactive_env, monkeypatch):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    message_id = proactive.queue_message(slug, user_id, "manual", "早", owner=user_id)

    def switch_off(*args):
        proactive.set_config(slug, owner=user_id, enabled=False)

    monkeypatch.setattr("server.safety_gate.check_output", switch_off)
    result = service.proactive_prepare_delivery(user_id, slug, message_id)
    assert result["deliverable"] is False
    assert result["content"] == ""
    assert len(proactive.pending_messages(user_id, slug)) == 1


def test_delivery_returns_only_requested_owned_pending_message(proactive_env):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    message_id = proactive.queue_message(
        slug, user_id, "manual", "可投递", owner=user_id
    )
    result = service.proactive_prepare_delivery(user_id, slug, message_id)
    assert result == {
        "deliverable": True,
        "message_id": message_id,
        "content": "可投递",
        "reason": "",
    }
    proactive.mark_delivered([message_id], user_id)
    assert (
        service.proactive_prepare_delivery(user_id, slug, message_id)["deliverable"]
        is False
    )


def test_delivery_cannot_read_another_users_queue(proactive_env):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    foreign_id = proactive.queue_message(
        slug, user_id + 1, "manual", "其他用户", owner=user_id
    )
    result = service.proactive_prepare_delivery(user_id, slug, foreign_id)
    assert result["deliverable"] is False
    assert result["content"] == ""


def test_delivery_cannot_read_another_mirrors_queue(proactive_env):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    foreign_id = proactive.queue_message(
        "othermirror", user_id, "manual", "另一个镜像", owner=user_id
    )
    result = service.proactive_prepare_delivery(user_id, slug, foreign_id)
    assert result["deliverable"] is False
    assert result["content"] == ""


def test_delivery_stops_when_health_blocks(proactive_env, monkeypatch):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    message_id = proactive.queue_message(slug, user_id, "manual", "早", owner=user_id)
    monkeypatch.setattr(proactive, "health_blocks", lambda _uid: "冷静期")
    result = service.proactive_prepare_delivery(user_id, slug, message_id)
    assert result["deliverable"] is False
    assert result["reason"] == "冷静期"
    assert result["content"] == ""
    assert len(proactive.pending_messages(user_id, slug)) == 1


def test_status_does_not_review_generate_or_charge(proactive_env, monkeypatch):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    proactive.queue_message(slug, user_id, "manual", "早", owner=user_id)

    def unexpected(*args, **kwargs):
        raise AssertionError("status 必须保持纯查询")

    monkeypatch.setattr(service, "_get_engine", unexpected)
    monkeypatch.setattr("server.billing_gate.reserve", unexpected)
    monkeypatch.setattr("server.safety_gate.check_output", unexpected)
    assert service.proactive_status(user_id, slug)["pending_count"] == 1


def test_delivery_review_error_is_fail_closed(proactive_env, monkeypatch):
    from core import proactive
    from mcp_server import service

    user_id, slug = proactive_env
    message_id = proactive.queue_message(slug, user_id, "manual", "早", owner=user_id)

    def review_failure(*args):
        raise RuntimeError("synthetic reviewer failure")

    monkeypatch.setattr("server.safety_gate.check_output", review_failure)
    with pytest.raises(service.ServiceError) as error:
        service.proactive_prepare_delivery(user_id, slug, message_id)
    assert error.value.code == "review_unavailable"
    assert len(proactive.pending_messages(user_id, slug)) == 1


@pytest.mark.parametrize("message_id", [True, "1", 0, -1, None])
def test_delivery_rejects_invalid_message_id(proactive_env, message_id):
    from mcp_server import service

    with pytest.raises(service.ServiceError) as error:
        service.proactive_prepare_delivery(*proactive_env, message_id)
    assert error.value.code == "invalid_argument"


@pytest.mark.parametrize("blocked", [False, True], ids=["settled", "review-rejected"])
def test_proactive_generation_uses_real_billing(proactive_env, monkeypatch, blocked):
    """真实账本贯通：生成时已预扣，成功结算，审核拒绝退回余额。"""
    from core import proactive
    from core.billing import balance_status, current_period, ensure_account
    from mcp_server import service
    from server.auth import _get_conn

    user_id, slug = proactive_env
    monkeypatch.setattr("config.INITIAL_BALANCE_MICROS", 100_000)
    monkeypatch.setattr("config.TURN_PRICE_MICROS", 20_000)
    monkeypatch.setattr(proactive, "decide_trigger", lambda *a, **k: "morning")
    # 使用真实 check_output 和审计写入，只替换词表为无害的测试标记。
    monkeypatch.setattr("core.safety.moderation._provider", None)
    monkeypatch.setattr(
        "core.safety.moderation._wordlist_cache",
        [{"name": "test", "severity": "block", "terms": ["test-blocked-output"]}],
    )
    account_id = ensure_account(user_id)
    before = balance_status(account_id)["balance_micros"]
    model_calls = []

    class Engine:
        model = "billing-test-model"
        last_provider = "billing-test-provider"

        def chat(self, message, history):
            model_calls.append(message)
            assert balance_status(account_id)["balance_micros"] == before - 20_000
            with _get_conn() as conn:
                quota = conn.execute(
                    "SELECT turns_reserved FROM quotas WHERE account_id = ? AND period = ?",
                    (account_id, current_period()),
                ).fetchone()
            assert quota["turns_reserved"] == 1
            content = "test-blocked-output" if blocked else "今天也好好休息"
            usage = SimpleNamespace(
                prompt_tokens=1000, completion_tokens=200, cached_tokens=100
            )
            return content, [], usage

    monkeypatch.setattr(service, "_get_engine", lambda *_args: Engine())
    result = service.proactive_tick(user_id, slug)
    assert len(model_calls) == 1
    assert result["sent"] is not blocked
    expected_charge = 0 if blocked else 20_000
    assert balance_status(account_id)["balance_micros"] == before - expected_charge

    with _get_conn() as conn:
        quota = conn.execute(
            "SELECT turns_reserved, turns_settled FROM quotas"
            " WHERE account_id = ? AND period = ?",
            (account_id, current_period()),
        ).fetchone()
        usage_rows = conn.execute(
            "SELECT * FROM usage_records WHERE account_id = ?", (account_id,)
        ).fetchall()
        ledger_rows = conn.execute(
            "SELECT kind, amount_micros FROM ledger WHERE account_id = ?", (account_id,)
        ).fetchall()

    assert quota["turns_reserved"] == 0
    assert quota["turns_settled"] == (0 if blocked else 1)
    assert len(proactive.pending_messages(user_id, slug)) == (0 if blocked else 1)
    if blocked:
        assert result["content"] == ""
        assert usage_rows == []
        assert ledger_rows == []
    else:
        assert len(usage_rows) == 1
        row = usage_rows[0]
        assert (row["user_id"], row["slug"], row["kind"]) == (
            user_id,
            slug,
            "proactive",
        )
        assert (row["provider"], row["model"]) == (
            "billing-test-provider",
            "billing-test-model",
        )
        assert (
            row["prompt_tokens"],
            row["completion_tokens"],
            row["cached_tokens"],
        ) == (
            1000,
            200,
            100,
        )
        assert [dict(row) for row in ledger_rows] == [
            {"kind": "consume", "amount_micros": -20_000}
        ]
