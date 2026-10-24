"""BYOK 运行时边界：只用离线客户端，不读取真实凭据或访问供应商。"""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock
import json
import traceback

import pytest


def _cfg(owner, revision=1, *, kind="llm", locked=False):
    return {
        "api_key": f"account-{owner}-key-{revision}",
        "base_url": "https://models.example.com/v1",
        "model": f"{kind}-model", "provider": "custom", "revision": revision,
        "locked": locked,
    }


@pytest.fixture
def clients(monkeypatch):
    import config
    from core import llm_router

    config._account_llm_clients.clear()
    llm_router.reset_for_tests()
    seen = []

    def client(**options):
        def create(**request):
            seen.append((options["api_key"], request.get("model")))
            return options["api_key"]
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr("openai.OpenAI", client)
    monkeypatch.setattr("core.provider_transport.build_provider_http_client", lambda url: object())
    yield seen
    config._account_llm_clients.clear()
    llm_router.reset_for_tests()


def test_account_clients_and_routers_are_concurrent_and_snapshot_scoped(clients, monkeypatch):
    import config
    from core.llm_router import get_router

    revisions = {1: 1, 2: 1}
    monkeypatch.setattr("server.model_settings.get_model_config", lambda owner, kind: _cfg(owner, revisions[owner], kind=kind))
    monkeypatch.setattr(config, "LLM_API_KEY", "station-owner-secret")
    monkeypatch.setattr(config, "LLM_PROVIDERS", '[{"api_key":"station-backup-secret"}]')

    def invoke(owner):
        cfg = config.get_llm_config(owner)
        value, _ = get_router(owner, cfg).call(lambda client, model, **kw: client.chat.completions.create(model=model))
        return value

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(invoke, [1, 2] * 10)) == ["account-1-key-1", "account-2-key-1"] * 10
    old_snapshot = config.get_llm_config(1)
    revisions[1] = 2
    assert config.get_llm_client(1, old_snapshot).chat.completions.create(model="llm-model") == "account-1-key-1"
    assert invoke(1) == "account-1-key-2"
    assert config._account_llm_clients.keys() == [(2, 1), (1, 1), (1, 2)]
    assert all("station" not in key for key, _ in clients)


def test_missing_account_settings_never_fall_back_to_station(monkeypatch):
    import config
    from server.model_settings import MissingModelKey

    monkeypatch.setattr(config, "LLM_API_KEY", "station-secret")
    monkeypatch.setattr(config, "EMBEDDING_API_KEY", "station-embedding-secret")
    constructor = MagicMock()
    monkeypatch.setattr("openai.OpenAI", constructor)
    def missing(*args, **kwargs):
        raise MissingModelKey("请先配置账号模型")
    monkeypatch.setattr("server.model_settings.get_model_config", missing)
    for get in (config.get_llm_config, config.get_llm_client, config.get_embedding_config):
        with pytest.raises(MissingModelKey):
            get(7)
    assert config.get_llm_config()["api_key"] == "station-secret"
    constructor.assert_not_called()


def test_account_provider_failure_never_uses_station_fallback(clients, monkeypatch, caplog):
    import config
    from core.llm_router import AllProvidersFailed, get_router

    seen = []
    def client(**options):
        seen.append(options["api_key"])
        def fail(**kwargs):
            error = RuntimeError(options["api_key"] + " private-prompt")
            error.status_code = 503
            raise error
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
    monkeypatch.setattr("openai.OpenAI", client)
    monkeypatch.setattr(config, "LLM_PROVIDERS", '[{"name":"station","api_key":"station-secret"}]')
    cfg = _cfg(9)
    with pytest.raises(AllProvidersFailed) as caught:
        get_router(9, cfg).call(lambda client, model, **kw: client.chat.completions.create(model=model))
    assert seen == ["account-9-key-1"]
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert "account-9-key-1" not in rendered and "private-prompt" not in rendered


@pytest.mark.parametrize("stream", [False, True])
def test_provider_echo_does_not_escape_error_or_traceback(stream, caplog):
    from core.model_client import ModelProviderError, account_client

    poison = "secret-key-and-private-chat"
    error = RuntimeError(poison)
    error.status_code = 401
    def chunks():
        yield SimpleNamespace(choices=[])
        raise error
    def create(**kwargs):
        if kwargs.get("stream"):
            return chunks()
        raise error
    client = account_client(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(ModelProviderError) as caught:
        result = client.chat.completions.create(stream=stream)
        if stream:
            list(result)
    rendered = "".join(traceback.format_exception(caught.value))
    assert poison not in rendered + caplog.text
    assert caught.value.status_code == 401


def test_embedding_provider_echo_is_redacted_before_retry_logs(monkeypatch, caplog):
    from memory.embedder import Embedder, EmbeddingError

    error = RuntimeError("embedding-secret private-message")
    error.status_code = 401
    create = MagicMock(side_effect=error)
    monkeypatch.setattr("memory.embedder.OpenAI", lambda **kw: SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    monkeypatch.setattr("core.provider_transport.build_provider_http_client", lambda url: object())
    cfg = _cfg(1, kind="embedding")
    embedder = Embedder(cfg["api_key"], cfg["base_url"], cfg["model"], owner=1, model_config=cfg)
    with pytest.raises(EmbeddingError) as caught:
        embedder.embed_one("private-message")
    assert create.call_count == 1
    assert "embedding-secret" not in caplog.text + str(caught.value)
    assert "private-message" not in caplog.text + str(caught.value)


def test_engine_cache_reads_latest_settings_revision(monkeypatch):
    import server.routes as routes

    revision = [1]
    factory = MagicMock(side_effect=lambda slug, owner: (object(), None, None))
    monkeypatch.setattr(routes, "_check_exe_ready", lambda *args: None)
    monkeypatch.setattr("server.model_settings.get_settings_revision", lambda owner: revision[0])
    monkeypatch.setattr("core.factory.create_engine_and_store", factory)
    routes._engine_cache.clear()
    one = routes._get_engine("same", 1)
    assert routes._get_engine("same", 1) is one
    assert routes._get_engine("same", 2) is not one
    revision[0] = 2
    assert routes._get_engine("same", 1) is not one
    routes._engine_cache.clear()


def test_index_requires_trusted_identity_and_locks_only_on_write(monkeypatch):
    from core.factory import check_embedding_identity
    from core.mirror_store import mirror_store
    from server.model_settings import EmbeddingModelLocked

    vector = MagicMock()
    vector.count.return_value = 2
    cfg = _cfg(1, kind="embedding", locked=True)
    lock = MagicMock()
    monkeypatch.setattr("server.model_settings.lock_embedding_model", lock)
    with pytest.raises(EmbeddingModelLocked):
        check_embedding_identity("first", 1, vector, cfg)
    lock.assert_not_called()
    vector.count.return_value = 0
    check_embedding_identity("first", 1, vector, cfg)
    lock.assert_not_called()
    check_embedding_identity("first", 1, vector, cfg, initialize=True)
    lock.assert_called_once_with(1, 1)
    meta = mirror_store("first", 1).read_json("meta.json")
    assert meta["embedding_identity"] == {key: cfg[key] for key in ("provider", "model", "base_url")}
    assert "key" not in json.dumps(meta)
    vector.count.return_value = 2
    check_embedding_identity("first", 1, vector, cfg)
    with pytest.raises(EmbeddingModelLocked):
        check_embedding_identity("first", 1, vector, {**cfg, "base_url": "https://other.example.com/v1"})


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_missing_key_is_rejected_before_billing_or_model(path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import server.routes as routes
    from server.middleware import require_auth
    from server.model_settings import MissingModelKey

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_auth] = lambda: 1
    monkeypatch.setattr(routes, "_check_exe_access", lambda slug, owner: slug)
    monkeypatch.setattr(routes, "_check_exe_ready", lambda *args: None)
    for gate in ("check_crisis", "check_limit", "check_input"):
        monkeypatch.setattr(routes, gate, lambda *args: None)
    def missing(*args, **kwargs):
        raise MissingModelKey("请先配置账号模型")
    monkeypatch.setattr("server.model_settings.get_settings_revision", lambda owner: 1)
    reserve, engine = MagicMock(), MagicMock(side_effect=missing)
    monkeypatch.setattr(routes, "reserve", reserve)
    monkeypatch.setattr("core.factory.create_engine_and_store", engine)
    routes._engine_cache.clear()
    response = TestClient(app).post(path, json={"slug": "first", "message": "hello"})
    assert response.status_code == 409
    reserve.assert_not_called()
    engine.assert_called_once()


def test_worker_rejects_payload_owner_mismatch(monkeypatch):
    from core import tasks

    task = {"status": "queued", "task_type": "owner_test", "user_id": 1, "attempts": 0, "payload": '{"owner":2}'}
    handler, update = MagicMock(), MagicMock()
    monkeypatch.setitem(tasks._handlers, "owner_test", handler)
    monkeypatch.setattr(tasks, "get_task", lambda task_id: task)
    monkeypatch.setattr(tasks, "_update_task", update)
    tasks._run("offline")
    handler.assert_not_called()
    assert update.call_args.kwargs["status"] == "failed"
    assert "归属" in update.call_args.kwargs["error"]


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_real_engine_configuration_check_does_not_call_provider_when_quota_blocks(path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.mirror_store import mirror_store
    import server.routes as routes
    from server.billing_gate import QuotaBlocked
    from server.middleware import require_auth

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_auth] = lambda: 1
    store = mirror_store("first", 1)
    store.write_text("SKILL.md", "# 本地人格")
    store.write_json("meta.json", {"owner_user_id": 1, "pipeline_state": "completed"})
    monkeypatch.setattr(routes, "_check_exe_access", lambda slug, owner: slug)
    for gate in ("check_crisis", "check_limit", "check_input"):
        monkeypatch.setattr(routes, gate, lambda *args: None)
    monkeypatch.setattr("server.model_settings.get_settings_revision", lambda owner: 1)
    monkeypatch.setattr("server.model_settings.get_model_config", lambda owner, kind: _cfg(owner, kind=kind))
    monkeypatch.setattr("core.provider_transport.build_provider_http_client", lambda url: object())
    provider_request = MagicMock(side_effect=AssertionError("余额不足不能发送模型请求"))
    sdk = SimpleNamespace(embeddings=SimpleNamespace(create=provider_request), chat=SimpleNamespace(completions=SimpleNamespace(create=provider_request)))
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: sdk)
    monkeypatch.setattr("memory.embedder.OpenAI", lambda **kwargs: sdk)
    vector = MagicMock()
    vector.count.return_value = 0
    monkeypatch.setattr("core.factory.build_vector_store", lambda *args: vector)
    chat, stream = MagicMock(), MagicMock()
    monkeypatch.setattr("core.engine.ChatEngine.chat", chat)
    monkeypatch.setattr("core.engine.ChatEngine.chat_stream", stream)
    monkeypatch.setattr(routes, "reserve", MagicMock(side_effect=QuotaBlocked({"type": "insufficient_balance"})))
    routes._engine_cache.clear()
    response = TestClient(app).post(path, json={"slug": "first", "message": "hello"})
    assert response.status_code == 200 and "insufficient_balance" in response.text
    assert len(routes._engine_cache) == 1  # 实际构造完成，而不是把配置检查 mock 掉。
    chat.assert_not_called()
    stream.assert_not_called()
    provider_request.assert_not_called()
    routes._engine_cache.clear()
