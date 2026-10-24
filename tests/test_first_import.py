"""首次导入回归：真实解析/语料存储，外部模型与向量调用离线替身。"""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def first_run(tmp_path, monkeypatch):
    import config
    import server.auth as auth
    import pipeline.orchestrator as flow
    import server.routes as routes

    monkeypatch.setattr(auth, "DB_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "DB_PATH", tmp_path / "data" / "users.db")
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(config, "EXES_DIR", tmp_path / "exes")
    monkeypatch.setattr(config, "SINGLE_USER_MODE", False)
    cfg = {"api_key": "offline-test", "base_url": "http://127.0.0.1:9", "model": "test", "provider": "custom", "revision": 1, "locked": False}
    monkeypatch.setattr(config, "get_embedding_config", lambda owner=None: cfg)
    monkeypatch.setattr(config, "get_llm_config", lambda owner=None: cfg)
    monkeypatch.setattr(flow, "get_embedding_config", lambda owner=None: cfg)
    monkeypatch.setattr(flow, "get_llm_config", lambda owner=None: cfg)
    vector = MagicMock()
    vector.count.return_value = 0
    monkeypatch.setattr("server.model_settings.lock_embedding_model", lambda *args: None)
    monkeypatch.setattr("memory.vector_store.VectorStore", lambda **kw: vector)
    monkeypatch.setattr("memory.embedder.Embedder", lambda **kw: MagicMock(model_config=kw.get("model_config")))
    monkeypatch.setattr(flow, "VectorStore", lambda **kw: vector)
    monkeypatch.setattr(flow, "Embedder", MagicMock())
    memory = MagicMock(return_value="# 记忆\n周六去山顶看日出")
    persona = MagicMock(return_value="# 人物\n喜欢说好呀")
    monkeypatch.setattr(flow, "build_memory", memory)
    monkeypatch.setattr(flow, "build_persona", persona)
    monkeypatch.setattr(routes, "_login_limiter", None)
    routes._engine_cache.clear()
    from server.app import create_app

    client = TestClient(create_app())
    client.post("/api/auth/register", json={"username": "first_run", "password": "offline123"})
    login = client.post("/api/auth/login", json={"username": "first_run", "password": "offline123"})
    headers = {"Authorization": "Bearer " + login.json()["token"]}
    yield client, headers, tmp_path, memory, persona, vector
    routes._engine_cache.clear()


def _create(client, headers, slug="first"):
    response = client.post("/api/exes", headers=headers, json={
        "slug": slug, "name": "显示昵称", "answers": ["基本信息"], "defer_generation": True,
    })
    assert response.status_code == 200, response.text


def _messages():
    return [
        {"sender": "小林", "content": "周六去山顶看日出", "timestamp": "2026-10-01 08:00:00"},
        {"sender": "我", "content": "好呀，带上相机", "timestamp": "2026-10-01 08:01:00"},
    ]


def _upload_path(root, rows=None, suffix="json", text=None):
    import uuid

    path = root / "uploads" / uuid.uuid4().hex / f"chat.{suffix}"
    path.parent.mkdir(parents=True)
    path.write_text(text if text is not None else json.dumps(rows or _messages(), ensure_ascii=False))
    return path


def _run_import(path, target="小林"):
    from server.task_handlers import handle_import

    return handle_import(MagicMock(), slug="first", owner=1, source_path=str(path), target_name=target)


def test_draft_does_not_generate_or_overwrite(first_run):
    client, headers, root, memory, persona, _ = first_run
    _create(client, headers)
    assert not memory.called and not persona.called
    metadata = root / "exes" / "1" / "first" / "meta.json"
    before = metadata.read_bytes()
    assert json.loads(before)["pipeline_state"] == "awaiting_import"
    assert json.loads(before)["stage"] == "source"
    response = client.post("/api/exes", headers=headers, json={
        "slug": "first", "name": "不同名字", "defer_generation": True,
    })
    assert response.status_code == 409
    assert metadata.read_bytes() == before


def test_actual_chat_drives_first_memory_persona_and_skill(first_run):
    client, headers, root, memory, persona, _ = first_run
    _create(client, headers)
    path = _upload_path(root)
    result = _run_import(path)
    assert result["state"] == "completed" and result["messages"] == 2 and result["chunks"] > 0
    assert "周六去山顶看日出" in memory.call_args.args[1]
    assert "周六去山顶看日出" in persona.call_args.args[1]
    assert memory.call_args.kwargs["source_grounded"] is True
    assert persona.call_args.kwargs["source_grounded"] is True
    skill = (root / "exes" / "1" / "first" / "SKILL.md").read_text()
    assert "周六去山顶看日出" in skill and "喜欢说好呀" in skill
    assert not path.parent.exists()
    from core.corpus_store import load_messages

    messages = load_messages("first", 1)
    assert [m["sender"] for m in messages] == ["小林", "我"]
    assert [m["is_target"] for m in messages] == [True, False]


def test_source_stage_roundtrip_and_explicit_user_selection(first_run):
    client, headers, _, _, _, _ = first_run
    _create(client, headers)
    assert client.get("/api/exes/first/stage", headers=headers).json()["stage"] == "source"
    assert client.put("/api/exes/first/stage?stage=healing", headers=headers).status_code == 200
    assert client.get("/api/exes/first/stage", headers=headers).json()["stage"] == "healing"
    assert client.put("/api/exes/first/stage?stage=source", headers=headers).status_code == 200
    assert client.get("/api/exes/first/stage", headers=headers).json()["stage"] == "source"


@pytest.mark.parametrize("body", ["{}", "[]", "not json", '[{"content": " "}]'])
def test_invalid_import_cannot_be_success_or_call_model(first_run, body):
    client, headers, root, memory, persona, _ = first_run
    _create(client, headers)
    with pytest.raises((ValueError, RuntimeError)):
        _run_import(_upload_path(root, text=body))
    assert not memory.called and not persona.called
    assert not (root / "exes" / "1" / "first" / "SKILL.md").exists()


def test_wrong_target_rejected_without_archiving(first_run):
    client, headers, root, memory, _, _ = first_run
    _create(client, headers)
    with pytest.raises(ValueError, match="目标人物"):
        _run_import(_upload_path(root), target="不存在")
    assert not memory.called
    assert not (root / "exes" / "1" / "first" / "corpus.jsonl").exists()


def test_generation_failure_reupload_is_safe_and_does_not_reembed(first_run):
    client, headers, root, memory, _, vector = first_run
    _create(client, headers)
    memory.side_effect = RuntimeError("测试模型暂时不可用")
    with pytest.raises(RuntimeError, match="memory.md"):
        _run_import(_upload_path(root))
    from core.corpus_store import load_messages

    assert len(load_messages("first", 1)) == 2
    assert vector.ingest.call_count == 1
    memory.side_effect = None
    result = _run_import(_upload_path(root))
    assert result["state"] == "completed"
    assert len(load_messages("first", 1)) == 2
    assert vector.ingest.call_count == 1


def test_embedding_failure_reupload_deduplicates_corpus(first_run):
    client, headers, root, memory, _, vector = first_run
    _create(client, headers)
    vector.ingest.side_effect = RuntimeError("测试 Embedding 失败")
    with pytest.raises(RuntimeError, match="Embedding"):
        _run_import(_upload_path(root))
    assert not memory.called
    vector.ingest.side_effect = None
    assert _run_import(_upload_path(root))["state"] == "completed"
    from core.corpus_store import load_messages

    assert len(load_messages("first", 1)) == 2


def test_existing_ready_mirror_is_not_regenerated(first_run):
    client, headers, root, memory, persona, _ = first_run
    _create(client, headers)
    _run_import(_upload_path(root))
    memory.reset_mock()
    persona.reset_mock()
    before = (root / "exes" / "1" / "first" / "SKILL.md").read_bytes()
    _run_import(_upload_path(root, rows=[{"sender": "小林", "content": "今天去游泳"}]))
    assert not memory.called and not persona.called
    assert (root / "exes" / "1" / "first" / "SKILL.md").read_bytes() == before


def test_import_ownership_is_rechecked_by_worker(first_run):
    client, headers, root, memory, _, _ = first_run
    _create(client, headers)
    from server.task_handlers import handle_import

    with pytest.raises((FileNotFoundError, PermissionError)):
        handle_import(MagicMock(), slug="first", owner=2, source_path=str(_upload_path(root)), target_name="小林")
    assert not memory.called


def test_concurrent_generation_is_rejected(first_run):
    client, headers, root, memory, _, _ = first_run
    _create(client, headers)
    from pipeline.orchestrator import import_creation_lock

    with import_creation_lock("first", 1):
        with pytest.raises(RuntimeError, match="正在导入或生成"):
            _run_import(_upload_path(root))
    assert not memory.called


def test_draft_cannot_chat_and_config_has_no_secrets(first_run):
    client, headers, _, _, _, _ = first_run
    _create(client, headers)
    from server.routes import _get_engine
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        _get_engine("first", 1)
    assert caught.value.status_code == 409
    assert client.get("/api/import/config").status_code == 401
    response = client.get("/api/import/config", headers=headers)
    assert response.status_code == 200
    assert set(response.json()) == {
        "allowed_extensions", "max_upload_bytes", "third_party_data_policy_version",
        "llm_configured", "embedding_configured",
    }
    assert "offline-test" not in response.text and "127.0.0.1" not in response.text
    for endpoint in ("/api/chat", "/api/chat/stream"):
        response = client.post(endpoint, headers=headers, json={"slug": "first", "message": "你好"})
        assert response.status_code == 409, response.text


def test_import_route_keeps_owner_and_slug_guards(first_run):
    client, headers, root, _, _, _ = first_run
    _create(client, headers)
    from core.mirror_store import mirror_store

    # 平铺旧镜像仍需 owner 校验。
    mirror_store("other").write_json("meta.json", {"owner_user_id": 2})
    for slug, expected in (("other", 403), ("missing", 404), ("bad.slug", 400)):
        response = client.post(f"/api/exes/{slug}/import", headers=headers, files={"file": ("chat.json", b"{}")})
        assert response.status_code == expected, response.text
    response = client.post("/api/exes/first/import", files={"file": ("chat.json", b"{}")})
    assert response.status_code == 401


@pytest.mark.parametrize("filename,body", [("empty.json", b""), ("image.png", b"image")])
def test_empty_or_unsupported_upload_rejected_before_task(first_run, filename, body):
    client, headers, root, _, _, _ = first_run
    _create(client, headers)
    import config

    client.post("/api/consents", headers=headers, json={
        "policy_type": "third_party_data", "policy_version": config.THIRD_PARTY_DATA_POLICY_VERSION,
    })
    response = client.post("/api/exes/first/import", headers=headers, files={"file": (filename, body)})
    assert response.status_code == 400
    assert not list((root / "data" / "uploads").glob("*"))


@pytest.mark.parametrize("body", [
    "2026-10-01 08:00:00 小林(12345)\n周六去山顶看日出\n2026-10-01 08:01:00 我(67890)\n好呀",
    "2026-10-01 08:00:00 小林\n周六去山顶看日出\n2026-10-01 08:01:00 我\n好呀",
])
def test_qq_and_wechat_text_keep_speakers(first_run, body):
    client, headers, root, _, _, _ = first_run
    _create(client, headers)
    assert _run_import(_upload_path(root, suffix="txt", text=body))["state"] == "completed"
    from core.corpus_store import load_messages

    assert [m["is_target"] for m in load_messages("first", 1)] == [True, False]


def test_json_explicit_direction_and_wechat_text_parser(tmp_path):
    from parsers.wechat_parser import _normalize_messages, parse_wechatmsg_txt

    rows = [
        {"sender": "目标", "content": "A", "is_target": True},
        {"sender": "自己", "content": "B", "is_target": False},
        {"sender": "原昵称", "content": "C", "isSend": "0"},
        {"sender": "我", "content": "D", "isSend": 1},
    ]
    parsed = _normalize_messages(rows, "显示昵称")
    assert [r["is_target"] for r in parsed] == [True, False, True, False]
    assert [r["sender"] for r in parsed] == [r["sender"] for r in rows]
    path = tmp_path / "chat.txt"
    path.write_text("2026-10-01 08:00:00 小林\n你好\n2026-10-01 08:01:00 我\n好呀")
    assert [m["is_target"] for m in parse_wechatmsg_txt(str(path), "小林")] == [True, False]


def test_factory_loads_real_chat_engine_from_owners_namespace(tmp_path, monkeypatch):
    import config
    import core.factory as factory
    from core.mirror_store import mirror_store

    monkeypatch.setattr(config, "EXES_DIR", tmp_path / "exes")
    monkeypatch.setattr(factory, "get_embedding_config", lambda owner=None: {"api_key": ""})
    monkeypatch.setattr(config, "get_llm_config", lambda owner=None: {
        "model": "offline", "temperature": 0, "top_p": 1,
        "frequency_penalty": 0, "max_tokens": 32,
    })
    for owner, content in [(1, "第一位用户的记忆"), (2, "第二位用户的记忆")]:
        store = mirror_store("shared", owner)
        store.write_text("SKILL.md", content)
        store.write_json("meta.json", {"owner_user_id": owner, "pipeline_state": "completed"})
        engine, vector, embedder = factory.create_engine_and_store("shared", owner=owner)
        assert engine.owner == owner
        assert engine.skill_content == content
        assert engine.store.base_dir == tmp_path / "exes" / str(owner) / "shared"
        assert vector is None and embedder is None
