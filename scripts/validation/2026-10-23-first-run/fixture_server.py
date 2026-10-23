"""Real local HTTP app with deterministic, offline-only model adapters."""

from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace as NS


def run(config, port, workspace: Path):
    ROOT = workspace.resolve()
    import uvicorn
    from evals.ab_runner import MockEmbedder
    import memory.embedder

    class OfflineEmbedder(MockEmbedder):
        def __init__(self, *args, **kwargs):
            self.model = "offline-test-bigram-256"

        def embed(self, texts):
            with (ROOT / "model-calls.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps({"kind": "embedding", "texts": len(texts)}) + "\n")
            return super().embed(texts)

        def embed_one(self, text):
            return self.embed([text])[0]

    memory.embedder.Embedder = OfflineEmbedder
    config.LLM_API_KEY = "offline-fixture-no-real-key"
    config.EMBEDDING_API_KEY = "offline-fixture-no-real-key"
    config.LLM_MODEL = "offline-test-model"
    config.METEOR_STORE_SSO_ENABLED = True
    config.METEOR_STORE_PROXY_TOKEN = "local-fixture-only"
    config.PUBLIC_BASE_PATH = ""
    config.REQUIRE_PHONE_VERIFICATION = False
    config.REQUIRE_AGE_CONFIRMATION = False
    # Preserve production's 10 initial turns so wallet gating is exercised.
    calls_file = ROOT / "model-calls.jsonl"

    def complete(**kwargs):
        messages = kwargs.get("messages", [])
        last = str(messages[-1].get("content", "")) if messages else ""
        with calls_file.open("a", encoding="utf-8") as out:
            out.write(
                json.dumps(
                    {
                        "kind": "llm",
                        "stream": bool(kwargs.get("stream")),
                        "model": kwargs.get("model"),
                        "prompt_chars": sum(
                            len(str(m.get("content", ""))) for m in messages
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        text = "# 合成验收资料\n" + last[-3000:]
        usage = NS(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        if kwargs.get("stream"):
            text = "离线验收回复：合成记录里的小雨喜欢冰美式。此回复仅用于流程测试。"

            def stream():
                for part in (text[:16], text[16:]):
                    yield NS(choices=[NS(delta=NS(content=part))], usage=None)
                yield NS(choices=[], usage=usage)

            return stream()
        return NS(choices=[NS(message=NS(content=text))], usage=usage)

    config.get_llm_client = lambda: NS(chat=NS(completions=NS(create=complete)))
    from server.app import app

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
                "llm": "deterministic_stub",
                "embedding": "offline_bigrams",
                "storage": "real_sqlite_and_chroma_in_tmp",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
