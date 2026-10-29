"""MCP 接入层测试。"""

import os

import pytest

os.environ["EX_MEMORY_TEST"] = "1"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每次测试使用独立 SQLite 数据库。"""
    import server.auth as auth

    test_db = tmp_path / "users.db"
    monkeypatch.setattr(auth, "DB_PATH", test_db)
    monkeypatch.setattr(auth, "DB_DIR", test_db.parent)

    with auth._get_conn() as conn:
        for table in ("mcp_tokens", "tokens", "users", "external_identities"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    auth.init_db()
    yield
    if test_db.exists():
        test_db.unlink()


@pytest.fixture
def user_id():
    """注册并登录，返回整数 user_id。

    注意 login_user 返回的是会话令牌字符串而不是主键，
    必须再过一次 validate_token 才能拿到整数 id。
    """
    from server.auth import login_user, register_user, validate_token

    register_user("mcpuser", "password123")
    return validate_token(login_user("mcpuser", "password123"))


def test_issue_and_validate(user_id):
    from mcp_server import tokens

    token = tokens.issue(user_id, label="claude")
    assert token.startswith("exm_")
    assert tokens.validate(token) == user_id


def test_plaintext_not_stored(user_id):
    from server.auth import _get_conn
    from mcp_server import tokens

    token = tokens.issue(user_id, label="cursor")
    with _get_conn() as conn:
        rows = conn.execute("SELECT token FROM mcp_tokens").fetchall()
    assert all(row["token"] != token for row in rows)


def test_revoke_takes_effect(user_id):
    from mcp_server import tokens

    token = tokens.issue(user_id, label="astrbot")
    assert tokens.revoke(token[:12], user_id=user_id) == 1
    assert tokens.validate(token) is None


def test_revoke_is_scoped_to_owner(user_id):
    """一个人的吊销动作不能动到别人的令牌。"""
    from server.auth import register_user
    from mcp_server import tokens

    register_user("other", "password123")
    token = tokens.issue(user_id, label="mine")
    assert tokens.revoke(token[:12], user_id=user_id + 1) == 0
    assert tokens.validate(token) == user_id


def test_revoking_session_token_does_not_kill_mcp(user_id):
    """吊销会话令牌不该影响 MCP 令牌——这是分表的核心理由。"""
    from server.auth import revoke_token
    from mcp_server import tokens

    mcp_token = tokens.issue(user_id)
    revoke_token("some-session-token")
    assert tokens.validate(mcp_token) == user_id


def test_list_returns_no_plaintext(user_id):
    from mcp_server import tokens

    tokens.issue(user_id, label="a")
    rows = tokens.list_tokens(user_id)
    assert len(rows) == 1
    assert "token" not in rows[0]
    assert rows[0]["label"] == "a"


@pytest.fixture
def mirror(user_id):
    """建一个归属该用户的镜像，返回 slug。"""
    from core.mirror_store import mirror_store

    slug = "testmirror"
    store = mirror_store(slug, user_id)
    store.write_json("meta.json", {"owner_user_id": user_id, "display_name": "测试"})
    yield slug
    from mcp_server.service import invalidate_engine

    invalidate_engine()


def test_unknown_slug_is_not_found(user_id):
    from mcp_server.service import ServiceError, _resolve_slug

    # slug 不允许连字符，用合法但不存在的名字
    with pytest.raises(ServiceError) as exc:
        _resolve_slug("nosuchmirror", user_id)
    assert exc.value.code == "not_found"


def test_malformed_slug_is_invalid(user_id):
    from mcp_server.service import ServiceError, _resolve_slug

    with pytest.raises(ServiceError) as exc:
        _resolve_slug("../escape", user_id)
    assert exc.value.code == "invalid_slug"


def test_missing_owner_is_forbidden(user_id):
    """镜像存在但未绑定用户：必须是 403，不能伪装成 404。"""
    from core.mirror_store import mirror_store
    from mcp_server.service import ServiceError, _resolve_slug

    mirror_store("orphan", user_id).write_json("meta.json", {})
    with pytest.raises(ServiceError) as exc:
        _resolve_slug("orphan", user_id)
    assert exc.value.code == "forbidden"


def test_crisis_blocked_before_engine(user_id, mirror, monkeypatch):
    """命中危机时必须直接返回，连引擎都不许建。"""
    import mcp_server.service as service

    monkeypatch.setattr(
        "server.safety_gate.check_crisis",
        lambda *a, **k: {"type": "crisis", "message": "请寻求帮助"},
    )

    def _boom(*args, **kwargs):
        raise AssertionError("危机命中后不应构建引擎或调用模型")

    monkeypatch.setattr(service, "_get_engine", _boom)

    result = service.chat(user_id, mirror, "触发危机检测的输入")
    assert result["blocked"] is True
    assert result["reply"] == ""


def test_crisis_checked_before_input_moderation(user_id, mirror, monkeypatch):
    """危机检测必须排在内容审核之前。

    顺序反了的话，一条同时命中两者的求救信息会被当成违规挡回去，
    而这恰恰是最不能挡的一类。
    """
    import mcp_server.service as service

    calls = []
    monkeypatch.setattr(
        "server.safety_gate.check_crisis",
        lambda *a, **k: calls.append("crisis") or {"type": "crisis"},
    )
    monkeypatch.setattr(
        "server.safety_gate.check_input",
        lambda *a, **k: calls.append("input") or {"type": "blocked"},
    )

    service.chat(user_id, mirror, "随便说点什么")
    assert calls == ["crisis"]


def test_quota_not_consumed_when_blocked(user_id, mirror, monkeypatch):
    """被闸门拦下的请求没有调模型，就不该扣额度。"""
    import mcp_server.service as service

    monkeypatch.setattr(
        "server.safety_gate.check_input",
        lambda *a, **k: {"type": "blocked", "message": "违规"},
    )

    def _boom(*args, **kwargs):
        raise AssertionError("被审核拦截后不应预扣配额")

    monkeypatch.setattr("server.billing_gate.reserve", _boom)

    result = service.chat(user_id, mirror, "违规内容")
    assert result["blocked"] is True


class TestChatSuccessPath:
    """成功分支的返回契约。

    此前只测了被闸门拦下的情况，成功路径无人看守——
    而客户端恰恰是照着成功响应的字段写的。
    """

    @pytest.fixture(autouse=True)
    def stubbed(self, monkeypatch):
        import mcp_server.service as service

        class FakeEngine:
            model = "fake-model"

            def chat(self, message, history):
                return "我挺好的，你呢", [], None

        monkeypatch.setattr(service, "_get_engine", lambda slug, uid: FakeEngine())
        monkeypatch.setattr("server.safety_gate.check_crisis", lambda *a, **k: None)
        monkeypatch.setattr("server.safety_gate.check_input", lambda *a, **k: None)
        monkeypatch.setattr("server.safety_gate.check_output", lambda *a, **k: None)
        monkeypatch.setattr("server.usage_guard.check_limit", lambda *a, **k: None)
        monkeypatch.setattr("server.billing_gate.reserve", lambda *a, **k: (1, "ok"))
        monkeypatch.setattr("server.billing_gate.settle", lambda *a, **k: None)

    def test_success_returns_reply(self, user_id, mirror):
        from mcp_server.service import chat

        result = chat(user_id, mirror, "你好")
        assert result["reply"] == "我挺好的，你呢"
        assert result["slug"] == mirror

    def test_success_marks_not_blocked(self, user_id, mirror):
        """成功时必须显式给出 blocked=False。

        靠「键不存在」表达成功，会逼客户端去猜字段在不在。
        """
        from mcp_server.service import chat

        result = chat(user_id, mirror, "你好")
        assert result["blocked"] is False

    def test_success_has_no_notice(self, user_id, mirror):
        from mcp_server.service import chat

        result = chat(user_id, mirror, "你好")
        assert "notice" not in result


def test_correct_add_and_dedupe(user_id, mirror):
    from mcp_server.service import correct, list_corrections

    correct(user_id, mirror, "ta 不会说亲爱的")
    correct(user_id, mirror, "ta 不会说亲爱的")

    rows = list_corrections(user_id, mirror)
    assert rows["count"] == 1
    assert rows["corrections"][0]["count"] == 2


def test_correct_empty_rejected(user_id, mirror):
    from mcp_server.service import ServiceError, correct

    with pytest.raises(ServiceError) as exc:
        correct(user_id, mirror, "   ")
    assert exc.value.code == "empty_correction"


def test_correct_too_long_rejected(user_id, mirror):
    from mcp_server.service import ServiceError, correct

    with pytest.raises(ServiceError) as exc:
        correct(user_id, mirror, "x" * 501)
    assert exc.value.code == "too_long"


def test_status_is_read_only(user_id, mirror):
    """status 不能触发任何模型调用，否则客户端轮询会把成本打爆。"""
    import mcp_server.service as service

    def _boom(*args, **kwargs):
        raise AssertionError("status 不应构建引擎或调用模型")

    original = service._get_engine
    service._get_engine = _boom
    try:
        result = service.status(user_id, mirror)
    finally:
        service._get_engine = original

    assert result["slug"] == mirror
    assert result["message_count"] == 0


def test_list_mirrors_only_returns_owned(user_id, mirror):
    from mcp_server.service import list_mirrors

    result = list_mirrors(user_id)
    assert result["count"] == 1
    assert result["mirrors"][0]["slug"] == mirror


def test_list_mirrors_excludes_others(user_id, mirror):
    from core.mirror_store import mirror_store
    from mcp_server.service import list_mirrors

    mirror_store("someone-elses", user_id + 99).write_json(
        "meta.json", {"owner_user_id": user_id + 99}
    )
    result = list_mirrors(user_id)
    assert [m["slug"] for m in result["mirrors"]] == [mirror]


def test_tools_registered(user_id):
    from mcp_server import tokens
    from mcp_server.server import build_server

    mcp = build_server(user_id, token=tokens.issue(user_id))
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {
        "exmemory_list_mirrors",
        "exmemory_search_memory",
        "exmemory_chat",
        "exmemory_correct",
        "exmemory_list_corrections",
        "exmemory_reflect",
        "exmemory_status",
    } <= names


def test_invalid_token_refuses_to_start(monkeypatch):
    from mcp_server.server import TOKEN_ENV, resolve_user_id

    monkeypatch.setenv(TOKEN_ENV, "exm_bogus")
    with pytest.raises(RuntimeError, match="无效"):
        resolve_user_id()


def test_missing_token_does_not_fall_back_when_multi_user(monkeypatch):
    import config
    from mcp_server.server import TOKEN_ENV, resolve_user_id

    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setattr(config, "SINGLE_USER_MODE", False)
    with pytest.raises(RuntimeError, match="未设置"):
        resolve_user_id()


def test_single_user_mode_gets_stable_local_identity(monkeypatch):
    import config
    from mcp_server.server import TOKEN_ENV, resolve_user_id

    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.setattr(config, "SINGLE_USER_MODE", True)
    assert resolve_user_id() == resolve_user_id()


def test_network_transport_rejects_public_bind():
    """记忆内容是私密数据，绑定非回环地址必须直接拒绝。"""
    from mcp_server.server import main

    with pytest.raises(RuntimeError, match="只允许监听本机"):
        main(transport="sse", host="0.0.0.0")


def test_proactive_defaults_to_off(user_id, mirror):
    """默认关闭是产品决策：主动消息是打扰，必须由用户明确开启。"""
    from mcp_server import service

    data = service.proactive_status(user_id, mirror)
    assert data["config"]["enabled"] is False
    assert data["pending"] == []
    assert data["pending_count"] == 0


def test_proactive_status_is_read_only(user_id, mirror):
    from mcp_server import service

    service.proactive_status(user_id, mirror)
    assert service.proactive_status(user_id, mirror)["pending_count"] == 0


def test_proactive_config_roundtrip(user_id, mirror):
    from mcp_server import service

    data = service.proactive_config(user_id, mirror, enabled=True, max_per_day=3)
    assert data["config"]["enabled"] is True
    assert data["config"]["max_per_day"] == 3
    # 没传的字段必须保持原值，否则改一个开关会顺手重置免打扰时段
    assert data["config"]["quiet_start"] == 23
    assert data["config"]["quiet_end"] == 8

    again = service.proactive_status(user_id, mirror)
    assert again["config"]["enabled"] is True


def test_proactive_config_rejects_out_of_range(user_id, mirror):
    from mcp_server import service
    from mcp_server.service import ServiceError

    with pytest.raises(ServiceError) as exc:
        service.proactive_config(user_id, mirror, max_per_day=99)
    assert exc.value.code == "invalid_config"


def test_proactive_tick_explains_why_nothing_sent(user_id, mirror):
    """未开启时不能只返回空——客户端要能对用户解释。"""
    from mcp_server import service

    data = service.proactive_tick(user_id, mirror)
    assert data["sent"] is False
    assert data["trigger"] is None
    assert data["reason"]


def test_proactive_tick_skips_when_health_blocks(user_id, mirror, monkeypatch):
    """健康保护优先于一切主动性：即便开启且到点，也不生成。"""
    import core.proactive as proactive
    from mcp_server import service

    service.proactive_config(user_id, mirror, enabled=True)
    monkeypatch.setattr(proactive, "health_blocks", lambda uid: "guard_active")

    data = service.proactive_tick(user_id, mirror)
    assert data["sent"] is False
    assert data["blocked_reason"] == "guard_active"
    assert "健康保护" in data["reason"]


def test_proactive_ack_marks_delivered(user_id, mirror):
    from core.proactive import queue_message
    from mcp_server import service

    message_id = queue_message(mirror, user_id, "morning", "早上好", owner=user_id)
    assert service.proactive_status(user_id, mirror)["pending_count"] == 1

    data = service.proactive_ack(user_id, [message_id])
    assert data == {"requested": 1, "acknowledged": 1}
    assert service.proactive_status(user_id, mirror)["pending_count"] == 0


def test_proactive_ack_repeats_are_harmless(user_id, mirror):
    """重复确认不该报错：投递侧重试是常态。"""
    from core.proactive import queue_message
    from mcp_server import service

    message_id = queue_message(mirror, user_id, "night", "晚安", owner=user_id)
    service.proactive_ack(user_id, [message_id])
    second = service.proactive_ack(user_id, [message_id])
    assert second["acknowledged"] == 0


def test_proactive_ack_cannot_touch_other_users_messages(user_id, mirror):
    from core.proactive import queue_message
    from server.auth import login_user, register_user, validate_token
    from mcp_server import service

    register_user("otheruser", "password123")
    other_id = validate_token(login_user("otheruser", "password123"))
    message_id = queue_message(mirror, other_id, "morning", "早上好", owner=user_id)

    data = service.proactive_ack(user_id, [message_id])
    assert data["acknowledged"] == 0


def test_proactive_ack_rejects_non_integer(user_id):
    from mcp_server import service
    from mcp_server.service import ServiceError

    with pytest.raises(ServiceError) as exc:
        service.proactive_ack(user_id, ["abc"])
    assert exc.value.code == "invalid_argument"


def test_proactive_ack_rejects_oversized_batch(user_id):
    from mcp_server import service
    from mcp_server.service import ServiceError

    with pytest.raises(ServiceError) as exc:
        service.proactive_ack(user_id, list(range(101)))
    assert exc.value.code == "invalid_argument"


class TestProactiveTickSuccess:
    """生成路径：判定被打桩，只验编排与返回契约。

    decide_trigger 打桩而不是重测，是为了避免造出第二套判定——
    两套规则一旦漂移，插件绕过的就是健康保护。
    """

    @pytest.fixture(autouse=True)
    def stubbed(self, monkeypatch):
        import core.proactive as proactive
        import mcp_server.service as service

        class FakeEngine:
            def chat(self, message, history):
                return "好久没跟你说话了", [], None

        monkeypatch.setattr(service, "_get_engine", lambda slug, uid: FakeEngine())
        monkeypatch.setattr(proactive, "decide_trigger", lambda *a, **k: "silence")
        monkeypatch.setattr(proactive, "health_blocks", lambda uid: None)

    def test_tick_queues_and_returns_content(self, user_id, mirror):
        from mcp_server import service

        data = service.proactive_tick(user_id, mirror)
        assert data["sent"] is True
        assert data["trigger"] == "silence"
        assert data["content"] == "好久没跟你说话了"
        assert isinstance(data["message_id"], int)
        assert data["blocked_reason"] is None

        assert service.proactive_status(user_id, mirror)["pending_count"] == 1

    def test_empty_generation_is_not_queued(self, user_id, mirror, monkeypatch):
        """生成空串时不能排一条空消息进队列。"""
        import core.proactive as proactive
        from mcp_server import service

        monkeypatch.setattr(
            proactive, "compose_with_usage", lambda engine, trigger: ("   ", None)
        )

        data = service.proactive_tick(user_id, mirror)
        assert data["sent"] is False
        assert data["message_id"] is None
        assert service.proactive_status(user_id, mirror)["pending_count"] == 0

    def test_generation_failure_maps_to_readable_error(
        self, user_id, mirror, monkeypatch
    ):
        """没有 Key 时要说清楚，而不是抛一坨 traceback 给客户端。"""
        import core.proactive as proactive
        from mcp_server import service
        from mcp_server.service import ServiceError

        def boom(engine, trigger):
            raise RuntimeError("401 Unauthorized")

        monkeypatch.setattr(proactive, "compose_with_usage", boom)

        with pytest.raises(ServiceError) as exc:
            service.proactive_tick(user_id, mirror)
        assert exc.value.code == "model_unavailable"
        assert "LLM Key" in exc.value.message
