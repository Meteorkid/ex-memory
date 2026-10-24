"""Real local HTTP app with deterministic, offline-only model adapters."""

from __future__ import annotations
import json
import base64
import hashlib
import os
import threading
import time
from pathlib import Path


def _fixture_master_key(root: Path) -> str:
    directory = root / ".fixture-secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "model-settings.key"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        path.chmod(0o600)
    else:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"))
    return path.read_text(encoding="ascii").strip()


def run(config, port, workspace: Path):
    ROOT = workspace.resolve()
    import httpx
    import uvicorn
    from evals.ab_runner import MockEmbedder
    import core.provider_transport

    config.MODEL_SETTINGS_ENCRYPTION_KEY = _fixture_master_key(ROOT)
    config.LLM_API_KEY = "station-llm-sentinel-must-never-be-used"
    config.EMBEDDING_API_KEY = "station-embedding-sentinel-must-never-be-used"
    config.LLM_BASE_URL = "https://station.example.com/v1"
    config.EMBEDDING_BASE_URL = "https://station.example.com/v1"
    config.LLM_PROVIDERS = json.dumps([{
        "name": "station-sentinel", "api_key": config.LLM_API_KEY,
        "base_url": "https://station.example.com/v1", "model": "station-model",
    }])
    config.METEOR_STORE_SSO_ENABLED = True
    config.METEOR_STORE_PROXY_TOKEN = "local-fixture-only"
    config.PUBLIC_BASE_PATH = ""
    config.REQUIRE_PHONE_VERIFICATION = False
    config.REQUIRE_AGE_CONFIRMATION = False
    # Preserve production's 10 initial turns so wallet gating is exercised.
    calls_file = ROOT / "model-calls.jsonl"
    calls_lock = threading.Lock()
    offline_vectors = MockEmbedder()
    native_vectors = MockEmbedder()
    native_vectors.DIM = 1024

    def record(entry):
        with calls_lock, calls_file.open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def supplier_http(request):
        """仅替代最终 HTTP 出口；账号、协议适配、router 和 Embedder 均真实执行。"""
        path = request.url.path
        protocol = "anthropic" if path.endswith("/messages") else "gemini" if ":" in path else "openai"
        key = (request.headers.get("x-api-key") or request.headers.get("x-goog-api-key")
               or request.headers.get("Authorization", "").removeprefix("Bearer "))
        key_class = "synthetic_account" if key.startswith("fixture-") else "rejected"
        if key in (config.LLM_API_KEY, config.EMBEDDING_API_KEY):
            key_class = "station_sentinel"
        identity = hashlib.sha256(key.encode()).hexdigest()[:12]
        kwargs = json.loads(request.content)
        kind = "embedding" if path.endswith(("/embeddings", ":batchEmbedContents")) else "llm"
        messages = kwargs.get("messages", [])
        if protocol == "gemini" and kind == "llm":
            messages = [{"content": "".join(p.get("text", "") for p in item.get("parts", []))}
                        for item in kwargs.get("contents", [])]
        streaming = bool(kwargs.get("stream")) or path.endswith(":streamGenerateContent")
        entry = {
            "kind": kind, "identity": identity, "key_class": key_class,
            "protocol": protocol, "stream": streaming, "model": kwargs.get("model"),
            "host": request.url.host,
            "prompt_chars": sum(len(str(m.get("content", ""))) for m in messages),
        }
        if key_class != "synthetic_account":
            record({**entry, "accepted": False})
            return httpx.Response(401, json={"error": {"message": "离线验收仅接受 fixture- 开头的虚构 Key"}})
        record({**entry, "accepted": True})
        if kind == "embedding":
            if protocol == "gemini":
                texts = ["".join(p.get("text", "") for p in item["content"]["parts"])
                         for item in kwargs["requests"]]
                return httpx.Response(200, json={"embeddings": [
                    {"values": vector} for vector in native_vectors.embed(texts)
                ]})
            texts = kwargs.get("input", [])
            if isinstance(texts, str):
                texts = [texts]
            return httpx.Response(200, json={
                "object": "list", "model": kwargs.get("model"),
                "data": [{"object": "embedding", "index": i, "embedding": vector}
                         for i, vector in enumerate(offline_vectors.embed(texts))],
                "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
            })
        last = str(messages[-1].get("content", "")) if messages else ""
        text = f"# 合成验收资料（账号模型 {identity}）\n" + last[-3000:]
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        base = {"id": "fixture-" + identity, "created": int(time.time()), "model": kwargs.get("model")}
        if streaming:
            text = f"离线验收回复（账号模型 {identity}）：合成记录里的小雨喜欢冰美式。此回复仅用于流程测试。"
        if protocol == "anthropic":
            message = {"id": base["id"], "type": "message", "role": "assistant",
                       "model": kwargs["model"], "content": [{"type": "text", "text": text}],
                       "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 20}}
            if not streaming:
                return httpx.Response(200, json=message)
            events = [
                {"type": "message_start", "message": {**message, "content": [], "stop_reason": None}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                *[{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": part}} for part in (text[:16], text[16:])],
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}},
                {"type": "message_stop"},
            ]
            content = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)
        if protocol == "gemini":
            def chunk(part, finished=False):
                candidate = {"index": 0, "content": {"role": "model", "parts": [{"text": part}]}}
                if finished:
                    candidate["finishReason"] = "STOP"
                return {"candidates": [candidate], "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 120}}
            if not streaming:
                return httpx.Response(200, json=chunk(text, True))
            content = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in [chunk(text[:16]), chunk(text[16:], True)])
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)
        if streaming:
            events = [{**base, "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {"content": part}, "finish_reason": None}
            ]} for part in (text[:16], text[16:])]
            events.append({**base, "object": "chat.completion.chunk", "choices": [], "usage": usage})
            content = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content + "data: [DONE]\n\n")
        return httpx.Response(200, json={
            **base, "object": "chat.completion", "usage": usage,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        })

    def mock_provider_http_client(base_url):
        core.provider_transport.normalize_base_url(base_url)
        return httpx.Client(transport=httpx.MockTransport(supplier_http), follow_redirects=False, trust_env=False)

    core.provider_transport.build_provider_http_client = mock_provider_http_client
    from server.app import app
    from fastapi import Depends
    from server.middleware import require_auth

    @app.get("/__fixture__/state")
    def fixture_state(user_id: int = Depends(require_auth)):
        from server.routes import _engine_cache
        from core.llm_router import _account_routers
        from server.model_settings import get_settings_revision

        return {
            "user_id": user_id, "revision": get_settings_revision(user_id),
            "engine_keys": [key for key in _engine_cache.keys() if key[0] == user_id],
            "client_revisions": [key[1] for key in config._account_llm_clients.keys() if key[0] == user_id],
            "router_revisions": [key[1] for key in _account_routers.keys() if key[0] == user_id],
        }

    # A task-only loopback reverse proxy shim. Does not bypass owner or consent
    # checks; it supplies synthetic users just as the site's verified proxy does.
    @app.middleware("http")
    async def sandbox_proxy(request, call_next):
        headers = [
            (k, v)
            for k, v in request.scope["headers"]
            if k not in (b"x-ex-memory-user-id", b"x-ex-memory-proxy-token")
        ]
        sandbox_user = request.headers.get("x-sandbox-user", "browser-fixture-1")
        if sandbox_user != "anonymous":
            headers.extend(
                [
                    (b"x-ex-memory-user-id", sandbox_user.encode("ascii")),
                    (b"x-ex-memory-proxy-token", b"local-fixture-only"),
                ]
            )
        request.scope["headers"] = headers
        return await call_next(request)

    samples = ROOT / "samples"
    samples.mkdir(exist_ok=True)
    rows = []
    topics = [
        "我只喝冰美式，不喜欢拿铁的奶味。",
        "周六下午三点去公园散步。",
        "我喜欢雾霾蓝，窗帘就是这个颜色。",
        "这次一起去旧书店，想找一本天文书。",
    ]
    for i in range(12):
        rows.extend(
            [
                {
                    "sender": "小雨",
                    "content": topics[i % len(topics)],
                    "isSend": 0,
                    "formattedTime": f"2026-10-01 10:{i:02d}:00",
                },
                {
                    "sender": "我",
                    "content": f"好的，我记下了。合成第{i + 1}轮。",
                    "isSend": 1,
                    "formattedTime": f"2026-10-01 10:{i:02d}:30",
                },
            ]
        )
    (samples / "synthetic-wechat.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (samples / "empty.json").write_text("[]", encoding="utf-8")
    (samples / "invalid.json").write_text("{ invalid JSON", encoding="utf-8")
    (samples / "synthetic-qq.txt").write_text(
        "\n\n".join(
            f"{r['formattedTime']} {r['sender']}({'10001' if r['isSend'] == 0 else '10002'})\n{r['content']}"
            for r in rows
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "url": f"http://127.0.0.1:{port}/",
                "mode": "isolated_proxy_fixture",
                "samples": str(samples),
                "consent_preaccepted": False,
                "llm": "real_account_getter_router_sdk_with_mock_http",
                "embedding": "real_embedder_sdk_with_mock_http_bigrams",
                "account_settings": "real_encrypted_sqlite_per_user",
                "browser_user": "browser-fixture-1",
                "provider_key_rule": "synthetic fixture- prefix only",
                "storage": "real_sqlite_and_chroma_in_tmp",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
