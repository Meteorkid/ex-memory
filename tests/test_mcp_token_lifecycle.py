"""同一 MCP 实例必须持续验证启动令牌，不能把一次登录当成永久授权。"""

import asyncio

import pytest

pytest.importorskip("mcp", reason="MCP 是可选依赖，需 requirements-mcp.txt")


TOOL_ARGUMENTS = {
    "list_mirrors": {},
    "search_memory": {"slug": "demo", "query": "你好"},
    "chat": {"slug": "demo", "message": "你好"},
    "correct": {"slug": "demo", "content": "不要用昵称"},
    "list_corrections": {"slug": "demo"},
    "conversations": {"slug": "demo"},
    "conversation_messages": {"slug": "demo", "conversation_id": "web"},
    "reflect": {"slug": "demo"},
    "status": {"slug": "demo"},
    "proactive_status": {"slug": "demo"},
    "proactive_config": {"slug": "demo", "enabled": True},
    "proactive_tick": {"slug": "demo"},
    "proactive_prepare_delivery": {"slug": "demo", "message_id": 1},
    "proactive_ack": {"message_ids": [1]},
}


@pytest.fixture
def identity(tmp_path, monkeypatch):
    import config
    import server.auth as auth
    from mcp_server import tokens
    from mcp_server.server import TOKEN_ENV

    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "users.db")
    monkeypatch.setattr(auth, "DB_DIR", tmp_path)
    monkeypatch.setattr(config, "SINGLE_USER_MODE", False)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    auth.init_db()
    user_id = auth.get_or_create_external_user_id("mcp_test", "owner")
    return user_id, tokens.issue(user_id, label="lifecycle")


@pytest.fixture
def services(monkeypatch):
    from mcp_server import service

    calls = []

    for name in TOOL_ARGUMENTS:

        def record(user_id, *args, _name=name, **kwargs):
            calls.append((_name, user_id))
            return {"ok": True, "user_id": user_id}

        monkeypatch.setattr(service, name, record, raising=False)
    return calls


def call(mcp, name, arguments=None):
    return asyncio.run(
        mcp._tool_manager.call_tool(
            "exmemory_" + name,
            TOOL_ARGUMENTS[name] if arguments is None else arguments,
        )
    )


@pytest.mark.parametrize("name", TOOL_ARGUMENTS)
def test_every_tool_rejects_revoked_token_on_same_connection(
    identity, services, monkeypatch, name
):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_server import tokens
    from mcp_server.server import TOKEN_ENV, build_server

    user_id, token = identity
    monkeypatch.setenv(TOKEN_ENV, token)
    mcp = build_server(user_id)
    assert call(mcp, name)["user_id"] == user_id
    assert services == [(name, user_id)]

    tokens.revoke(token[:12], user_id=user_id)
    with pytest.raises(ToolError, match="authentication_required"):
        call(mcp, name)
    assert services == [(name, user_id)]


@pytest.mark.parametrize("now", ["2030-01-01 00:00:00", "2030-01-01 00:00:01"])
def test_expired_token_rejected_at_boundary(identity, services, monkeypatch, now):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_server import tokens
    from mcp_server.server import TOKEN_ENV, build_server
    from server.auth import _get_conn

    user_id, token = identity
    monkeypatch.setattr(tokens, "_utc_now_str", lambda: "2029-12-31 23:59:59")
    with _get_conn() as conn:
        conn.execute(
            "UPDATE mcp_tokens SET expires_at = ? WHERE token = ?",
            ("2030-01-01 00:00:00", tokens._hash(token)),
        )
        conn.commit()
    monkeypatch.setenv(TOKEN_ENV, token)
    mcp = build_server(user_id)
    call(mcp, "list_mirrors")

    monkeypatch.setattr(tokens, "_utc_now_str", lambda: now)
    with pytest.raises(ToolError, match="authentication_required"):
        call(mcp, "list_mirrors")
    assert services == [("list_mirrors", user_id)]


def test_environment_change_cannot_replace_connection_identity(
    identity, services, monkeypatch
):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_server import tokens
    from mcp_server.server import TOKEN_ENV, build_server
    from server.auth import get_or_create_external_user_id

    user_id, token = identity
    monkeypatch.setenv(TOKEN_ENV, token)
    mcp = build_server(user_id)
    other_id = get_or_create_external_user_id("mcp_test", "other")
    monkeypatch.setenv(TOKEN_ENV, tokens.issue(other_id))
    assert call(mcp, "list_mirrors")["user_id"] == user_id
    tokens.revoke(token[:12], user_id=user_id)

    with pytest.raises(ToolError, match="authentication_required"):
        call(mcp, "list_mirrors")
    assert services == [("list_mirrors", user_id)]


def test_token_cannot_change_owner_in_live_connection(identity, services, monkeypatch):
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_server import tokens
    from mcp_server.server import TOKEN_ENV, build_server
    from server.auth import _get_conn, get_or_create_external_user_id

    user_id, token = identity
    monkeypatch.setenv(TOKEN_ENV, token)
    mcp = build_server(user_id)
    call(mcp, "list_mirrors")
    other_id = get_or_create_external_user_id("mcp_test", "other")
    with _get_conn() as conn:
        conn.execute(
            "UPDATE mcp_tokens SET user_id = ? WHERE token = ?",
            (other_id, tokens._hash(token)),
        )
        conn.commit()

    with pytest.raises(ToolError, match="authentication_required"):
        call(mcp, "list_mirrors")
    assert services == [("list_mirrors", user_id)]


def test_invalid_token_never_falls_back_to_single_user(identity, services, monkeypatch):
    import config
    from mcp_server.server import build_server

    user_id, _token = identity
    monkeypatch.setattr(config, "SINGLE_USER_MODE", True)
    with pytest.raises(RuntimeError, match="无效"):
        build_server(user_id, token="exm_invalid")
    assert services == []


def test_missing_token_cannot_build_multi_user_server(identity, services):
    from mcp_server.server import build_server

    user_id, _token = identity
    with pytest.raises(RuntimeError, match="未设置"):
        build_server(user_id, token="")
    assert services == []


def test_mismatched_startup_identity_is_rejected(identity, services):
    from mcp_server.server import build_server

    user_id, token = identity
    with pytest.raises(RuntimeError, match="身份不匹配"):
        build_server(user_id + 1, token=token)
    assert services == []


def test_explicit_single_user_connection_keeps_local_identity(
    identity, services, monkeypatch
):
    import config
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp_server.server import TOKEN_ENV, build_server, resolve_user_id

    _user_id, token = identity
    monkeypatch.setattr(config, "SINGLE_USER_MODE", True)
    local_id = resolve_user_id(token="")
    mcp = build_server(local_id, token="")
    monkeypatch.setenv(TOKEN_ENV, token)
    assert call(mcp, "list_mirrors")["user_id"] == local_id
    monkeypatch.setattr(config, "SINGLE_USER_MODE", False)
    with pytest.raises(ToolError, match="authentication_required"):
        call(mcp, "list_mirrors")
    assert services == [("list_mirrors", local_id)]


def test_all_registered_tools_are_covered(identity, services):
    from mcp_server.server import build_server

    user_id, token = identity
    mcp = build_server(user_id, token=token)
    assert {tool.name for tool in mcp._tool_manager.list_tools()} == {
        "exmemory_" + name for name in TOOL_ARGUMENTS
    }
