"""原生协议只连接离线 MockTransport；验证请求形状、流终止、秘密与向量边界。"""

import json
import math
import traceback

import httpx
import pytest

from core.model_client import (
    IncompleteModelResponse,
    ModelProviderError,
    account_client,
)
from core.native_model_client import GEMINI_EMBEDDING_DIMENSION, NativeModelClient

SECRET = "synthetic-native-api-key-1234"
MESSAGES = [
    {"role": "system", "content": "system-one"},
    {"role": "system", "content": "system-two"},
    {"role": "user", "content": "question-one"},
    {"role": "assistant", "content": "answer-one"},
    {"role": "user", "content": "question-two"},
]


@pytest.fixture
def make_client():
    clients = []

    def make(protocol, handler):
        http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
        clients.append(http)
        return NativeModelClient(
            protocol=protocol,
            api_key=SECRET,
            base_url="https://models.example.test/api",
            http_client=http,
        )

    yield make
    for client in clients:
        client.close()


def anthropic_result(**changes):
    result = {
        "content": [{"type": "text", "text": "你好"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 7, "output_tokens": 3},
    }
    result.update(changes)
    return result


def gemini_result(**changes):
    result = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": "你好"}]},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 7,
            "cachedContentTokenCount": 3,
            "candidatesTokenCount": 5,
            "thoughtsTokenCount": 4,
            "totalTokenCount": 16,
        },
    }
    result.update(changes)
    return result


class FragmentStream(httpx.SyncByteStream):
    def __init__(self, content, fail=False):
        self.content = content
        self.fail = fail
        self.closed = False

    def __iter__(self):
        for position in range(0, len(self.content), 3):
            yield self.content[position : position + 3]
        if self.fail:
            raise RuntimeError(SECRET + " private-message")

    def close(self):
        self.closed = True


def sse_payload(events):
    return b"".join(
        ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
        for event in events
    )


def stream_response(events, *, fail=False):
    body = FragmentStream(sse_payload(events), fail)
    return httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, stream=body
    ), body


def anthropic_events():
    return [
        {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 2,
                }
            },
        },
        {"type": "ping"},
        {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你"}},
        {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "hidden-thought"},
        },
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "好"}},
        {"type": "content_block_stop"},
        {"type": "new_unknown_event"},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]


def gemini_events():
    return [
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "hidden-thought", "thought": True},
                            {"text": "你"},
                        ]
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 1},
        },
        {
            "candidates": [
                {"content": {"parts": [{"text": "好"}]}, "finishReason": "STOP"}
            ]
        },
        {
            "usageMetadata": {
                "promptTokenCount": 7,
                "cachedContentTokenCount": 3,
                "candidatesTokenCount": 3,
                "thoughtsTokenCount": 2,
                "totalTokenCount": 12,
            }
        },
    ]


def test_anthropic_request_system_headers_default_limit_and_usage(make_client):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json=anthropic_result(
                usage={
                    "input_tokens": 7,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 2,
                    "cache_creation_input_tokens": 4,
                }
            ),
        )

    client = make_client("anthropic", handler)
    response = client.chat.completions.create(
        model="claude-test",
        messages=MESSAGES,
        temperature=0.3,
        top_p=0.9,
        frequency_penalty=0.6,
    )
    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://models.example.test/api/messages"
    assert request.headers["x-api-key"] == SECRET
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in request.headers
    assert SECRET not in str(request.url) + repr(client)
    assert body == {
        "model": "claude-test",
        "system": "system-one\n\nsystem-two",
        "messages": MESSAGES[2:],
        "max_tokens": 4096,
        "stream": False,
    }
    assert response.choices[0].message.content == "你好"
    assert vars(response.usage) == {
        "prompt_tokens": 13,
        "completion_tokens": 3,
        "total_tokens": 16,
    }


def test_gemini_request_roles_header_path_and_thinking_usage(make_client):
    seen = []
    client = make_client(
        "gemini",
        lambda request: (
            seen.append(request) or httpx.Response(200, json=gemini_result())
        ),
    )
    response = client.chat.completions.create(
        model="models/gemini-test",
        messages=MESSAGES,
        temperature=0.3,
        top_p=0.9,
        frequency_penalty=0.6,
        max_tokens=32,
    )
    request = seen[0]
    body = json.loads(request.content)
    assert (
        str(request.url)
        == "https://models.example.test/api/models/gemini-test:generateContent"
    )
    assert (
        request.headers["x-goog-api-key"] == SECRET
        and "Authorization" not in request.headers
    )
    assert SECRET not in str(request.url)
    assert body["systemInstruction"] == {
        "parts": [{"text": "system-one\n\nsystem-two"}]
    }
    assert [item["role"] for item in body["contents"]] == ["user", "model", "user"]
    assert [item["parts"][0]["text"] for item in body["contents"]] == [
        item["content"] for item in MESSAGES[2:]
    ]
    assert body["generationConfig"] == {
        "candidateCount": 1,
        "temperature": 0.3,
        "topP": 0.9,
        "frequencyPenalty": 0.6,
        "maxOutputTokens": 32,
    }
    assert vars(response.usage) == {
        "prompt_tokens": 7,
        "completion_tokens": 9,
        "total_tokens": 16,
    }


@pytest.mark.parametrize(
    "protocol,events,prompt,completion",
    [("anthropic", anthropic_events, 9, 3), ("gemini", gemini_events, 7, 5)],
)
def test_stream_fragmentation_text_and_final_cumulative_usage(
    make_client, protocol, events, prompt, completion
):
    response, body = stream_response(events())
    seen = []
    client = make_client(protocol, lambda request: seen.append(request) or response)
    chunks = list(
        account_client(client).chat.completions.create(
            model="test-model",
            messages=MESSAGES,
            stream=True,
            stream_options={"include_usage": True},
        )
    )
    assert (
        "".join(chunk.choices[0].delta.content for chunk in chunks if chunk.choices)
        == "你好"
    )
    assert chunks[-1].choices == []
    assert vars(chunks[-1].usage) == {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    assert body.closed
    if protocol == "gemini":
        assert dict(seen[0].url.params) == {"alt": "sse"}
        assert "key" not in seen[0].url.params


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_stream_close_before_and_after_first_chunk_releases_response(
    make_client, protocol
):
    factory = anthropic_events if protocol == "anthropic" else gemini_events
    response, body = stream_response(factory())
    seen = []
    client = account_client(
        make_client(protocol, lambda request: seen.append(request) or response)
    )
    unopened = client.chat.completions.create(
        model="test-model", messages=MESSAGES, stream=True
    )
    unopened.close()
    assert seen == []
    opened = client.chat.completions.create(
        model="test-model", messages=MESSAGES, stream=True
    )
    next(opened)
    opened.close()
    assert body.closed


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_stream_yields_first_text_without_waiting_for_end(make_client, protocol):
    prefix = anthropic_events()[:4] if protocol == "anthropic" else gemini_events()[:1]

    class LiveStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield sse_payload(prefix)
            pytest.fail("首个文本块前不能等待后续网络数据")

        def close(self):
            self.closed = True

    body = LiveStream()
    response = httpx.Response(200, stream=body)
    client = account_client(make_client(protocol, lambda request: response))
    stream = client.chat.completions.create(
        model="test-model", messages=MESSAGES, stream=True
    )
    assert next(stream).choices[0].delta.content == "你"
    stream.close()
    assert body.closed


@pytest.mark.parametrize("comments", [False, True])
def test_stream_byte_limits_stop_unbounded_lines_and_comments(
    make_client, monkeypatch, comments
):
    import core.native_model_client as native

    monkeypatch.setattr(native, "_MAX_RESPONSE_BYTES", 2048)
    monkeypatch.setattr(native, "_MAX_EVENT_BYTES", 512)

    class EndlessBody(httpx.SyncByteStream):
        received = 0
        closed = False

        def __iter__(self):
            for _ in range(100):
                self.received += 1
                yield b": " + b"x" * 125 + b"\n" if comments else b"x" * 128

        def close(self):
            self.closed = True

    body = EndlessBody()
    client = make_client("gemini", lambda request: httpx.Response(200, stream=body))
    with pytest.raises(IncompleteModelResponse):
        list(
            client.chat.completions.create(
                model="test-model", messages=MESSAGES, stream=True
            )
        )
    assert body.received <= 17 and body.closed


def test_anthropic_named_error_cannot_be_hidden_by_payload_type(make_client):
    payload = sse_payload(anthropic_events()[:4])
    payload += b'event: error\ndata: {"type":"ping"}\n\n'
    payload += sse_payload(anthropic_events()[-2:])
    body = FragmentStream(payload)
    client = account_client(
        make_client("anthropic", lambda request: httpx.Response(200, stream=body))
    )
    with pytest.raises(IncompleteModelResponse):
        list(
            client.chat.completions.create(
                model="test-model", messages=MESSAGES, stream=True
            )
        )
    assert body.closed


@pytest.mark.parametrize("status", [401, 403, 429, 503])
@pytest.mark.parametrize("stream", [False, True])
def test_http_errors_keep_status_without_exposing_provider_body(
    make_client, status, stream, caplog
):
    client = account_client(
        make_client(
            "gemini",
            lambda request: httpx.Response(
                status, json={"error": {"message": SECRET + " private-message"}}
            ),
        )
    )
    with pytest.raises(ModelProviderError) as error:
        result = client.chat.completions.create(
            model="gemini-test", messages=MESSAGES, stream=stream
        )
        if stream:
            list(result)
    assert error.value.status_code == status
    rendered = "".join(traceback.format_exception(error.value)) + caplog.text
    assert SECRET not in rendered and "private-message" not in rendered


@pytest.mark.parametrize(
    "variant", ["eof", "error", "max_tokens", "refusal", "empty", "network", "bad_json"]
)
@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_incomplete_streams_never_return_fake_success(
    make_client, protocol, variant, caplog
):
    events = anthropic_events() if protocol == "anthropic" else gemini_events()
    if variant == "eof":
        events = events[:4] if protocol == "anthropic" else events[:1]
    elif variant == "error":
        events = events[:4] if protocol == "anthropic" else events[:1]
        events.append(
            {"type": "error", "error": {"message": SECRET + " private-message"}}
        )
    elif variant in ("max_tokens", "refusal"):
        if protocol == "anthropic":
            events[-2]["delta"]["stop_reason"] = (
                "max_tokens" if variant == "max_tokens" else "refusal"
            )
        else:
            events[1]["candidates"][0]["finishReason"] = (
                "MAX_TOKENS" if variant == "max_tokens" else "SAFETY"
            )
    elif variant == "empty":
        events = (
            [events[0], events[-2], events[-1]]
            if protocol == "anthropic"
            else [{"candidates": [{"finishReason": "STOP"}]}]
        )
    elif variant == "network":
        events = events[:4] if protocol == "anthropic" else events[:1]
    response, body = stream_response(events, fail=variant == "network")
    if variant == "bad_json":
        body.content = b'data: {"broken": "' + SECRET.encode() + b"\n\n"
    client = account_client(make_client(protocol, lambda request: response))
    with pytest.raises(IncompleteModelResponse) as error:
        list(
            client.chat.completions.create(
                model="test-model", messages=MESSAGES, stream=True
            )
        )
    assert error.value.status_code == 422 and body.closed
    rendered = "".join(traceback.format_exception(error.value)) + caplog.text
    assert SECRET not in rendered and "private-message" not in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"candidates": []},
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"text": "thought", "thought": True}]},
                }
            ]
        },
        {
            "candidates": [
                {
                    "finishReason": "MAX_TOKENS",
                    "content": {"parts": [{"text": "partial"}]},
                }
            ]
        },
    ],
)
def test_gemini_blocked_empty_or_truncated_response_is_rejected(make_client, payload):
    client = make_client("gemini", lambda request: httpx.Response(200, json=payload))
    with pytest.raises(IncompleteModelResponse):
        client.chat.completions.create(model="gemini-test", messages=MESSAGES)


@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_missing_usage_stays_none_so_engine_can_estimate(make_client, protocol):
    result = (
        anthropic_result(usage=None)
        if protocol == "anthropic"
        else gemini_result(usageMetadata=None)
    )
    client = make_client(protocol, lambda request: httpx.Response(200, json=result))
    assert (
        client.chat.completions.create(model="test-model", messages=MESSAGES).usage
        is None
    )


@pytest.mark.parametrize(
    "messages,extra",
    [
        ([{"role": "tool", "content": "keep"}], {}),
        ([{"role": "user", "content": [{"type": "text", "text": "keep"}]}], {}),
        ([{"role": "user", "content": "keep", "tool_calls": []}], {}),
        (
            [
                {"role": "user", "content": "keep"},
                {"role": "system", "content": "later-system"},
            ],
            {},
        ),
        (MESSAGES, {"tools": []}),
        (MESSAGES, {"stream_options": {"other": True}}),
    ],
)
@pytest.mark.parametrize("protocol", ["anthropic", "gemini"])
def test_unsupported_input_is_rejected_before_request(
    make_client, messages, extra, protocol
):
    seen = []
    client = make_client(
        protocol, lambda request: seen.append(request) or httpx.Response(200)
    )
    with pytest.raises(ModelProviderError):
        client.chat.completions.create(model="test-model", messages=messages, **extra)
    assert seen == []


@pytest.mark.parametrize(
    "model",
    [
        "models/../bad",
        "../../admin",
        "models/x:generateContent",
        "x?key=secret",
        "https://evil.example",
    ],
)
def test_gemini_model_identifier_cannot_escape_endpoint(make_client, model):
    seen = []
    client = make_client(
        "gemini", lambda request: seen.append(request) or httpx.Response(200)
    )
    with pytest.raises(ModelProviderError):
        client.chat.completions.create(model=model, messages=MESSAGES)
    with pytest.raises(ModelProviderError):
        client.embeddings.create(model=model, input=["text"])
    assert seen == []


def vector(*prefix):
    return list(prefix) + [0.0] * (GEMINI_EMBEDDING_DIMENSION - len(prefix))


def test_gemini_embeddings_batch_order_dimension_and_normalization(make_client):
    seen = []
    client = make_client(
        "gemini",
        lambda request: (
            seen.append(request)
            or httpx.Response(
                200,
                json={
                    "embeddings": [
                        {"values": vector(3, 4)},
                        {"values": vector(0, 0, -2)},
                    ]
                },
            )
        ),
    )
    result = client.embeddings.create(
        model="models/gemini-embedding-001", input=["first", "second"]
    )
    payload = json.loads(seen[0].content)
    assert seen[0].url.path.endswith("/models/gemini-embedding-001:batchEmbedContents")
    assert payload == {
        "requests": [
            {
                "model": "models/gemini-embedding-001",
                "content": {"parts": [{"text": text}]},
                "embedContentConfig": {"outputDimensionality": 1024},
            }
            for text in ("first", "second")
        ]
    }
    assert result.data[0].embedding[:2] == pytest.approx([0.6, 0.8])
    assert result.data[1].embedding[2] == -1
    assert all(
        len(item.embedding) == 1024
        and math.fsum(value * value for value in item.embedding) == pytest.approx(1.0)
        for item in result.data
    )


@pytest.mark.parametrize(
    "embeddings",
    [
        [],
        [{"values": [1, 2]}],
        [{"values": vector()}],
        [{"values": vector(float("nan"))}],
        [{"values": vector(float("inf"))}],
        [{"values": vector(True)}],
        [{"values": vector(1)}, {"values": vector(1)}],
    ],
)
def test_embedding_bad_shape_zero_nonfinite_or_count_is_rejected(
    make_client, embeddings
):
    # json.dumps 可生成 NaN/Infinity，用于模拟不合规供应商输出。
    client = make_client(
        "gemini",
        lambda request: httpx.Response(
            200, content=json.dumps({"embeddings": embeddings})
        ),
    )
    with pytest.raises(IncompleteModelResponse):
        client.embeddings.create(model="gemini-embedding-001", input=["text"])


def test_config_uses_account_revision_for_native_clients(monkeypatch):
    import config

    requests, http_clients = [], []

    def make_http(base_url):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    requests.append(request)
                    or httpx.Response(200, json=anthropic_result())
                )
            ),
            trust_env=False,
        )
        http_clients.append(client)
        return client

    monkeypatch.setattr("core.provider_transport.build_provider_http_client", make_http)
    config._account_llm_clients.clear()
    try:
        first = {
            "protocol": "anthropic",
            "api_key": SECRET,
            "base_url": "https://models.example.test/v1",
            "revision": 1,
        }
        old = config.get_llm_client(1, first)
        assert config.get_llm_client(1, first) is old
        changed = config.get_llm_client(
            1, {**first, "api_key": "synthetic-new-key-5678", "revision": 2}
        )
        other = config.get_llm_client(
            2, {**first, "api_key": "synthetic-other-key-9012"}
        )
        for client in (old, changed, other):
            assert (
                client.chat.completions.create(model="claude-test", messages=MESSAGES)
                .choices[0]
                .message.content
                == "你好"
            )
        assert [request.headers["x-api-key"] for request in requests] == [
            SECRET,
            "synthetic-new-key-5678",
            "synthetic-other-key-9012",
        ]
    finally:
        config._account_llm_clients.clear()
        for client in http_clients:
            client.close()


def test_embedder_selects_native_gemini_without_openai_fallback(monkeypatch):
    from memory.embedder import Embedder

    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"embeddings": [{"values": vector(1)}]}
            )
        ),
        trust_env=False,
    )
    monkeypatch.setattr(
        "core.provider_transport.build_provider_http_client", lambda base: http
    )

    def unexpected(**kwargs):
        pytest.fail("Gemini native must not create an OpenAI client")

    monkeypatch.setattr("memory.embedder.OpenAI", unexpected)
    try:
        embedder = Embedder(
            SECRET,
            "https://models.example.test/v1beta",
            "gemini-embedding-001",
            owner=1,
            model_config={"protocol": "gemini"},
        )
        assert embedder.embed_one("text") == vector(1)
    finally:
        http.close()
