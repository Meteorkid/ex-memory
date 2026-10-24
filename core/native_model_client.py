"""Anthropic/Gemini 原生文本协议，向现有管线提供最小 OpenAI 形状。"""

import json
import math
import re
from types import SimpleNamespace

import httpx

from core.model_client import IncompleteModelResponse, ModelProviderError

GEMINI_EMBEDDING_DIMENSION = 1024
_GEMINI_MODEL = re.compile(r"(?:models/)?([A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z")
_ANTHROPIC_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_EVENT_BYTES = 1024 * 1024


def _messages(messages):
    if not isinstance(messages, list) or not messages:
        raise ModelProviderError(400)
    system, turns = [], []
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise ModelProviderError(400)
        role, content = message["role"], message["content"]
        if role not in ("system", "user", "assistant") or not isinstance(content, str):
            raise ModelProviderError(400)
        if role == "system":
            # 旧版 Anthropic 只支持顶层 system，不能静默重排中途插入的系统消息。
            if turns:
                raise ModelProviderError(400)
            system.append(content)
        else:
            turns.append({"role": role, "content": content})
    if not turns:
        raise ModelProviderError(400)
    return "\n\n".join(system), turns


def _count(usage, name):
    value = usage.get(name, 0)
    if type(value) is not int or value < 0:
        raise IncompleteModelResponse()
    return value


def _usage(usage, protocol):
    if usage is None or usage == {}:
        return None
    if not isinstance(usage, dict):
        raise IncompleteModelResponse()
    if protocol == "anthropic":
        if not set(usage).intersection(
            {
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            }
        ):
            return None
        prompt = sum(
            _count(usage, key)
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )
        completion = _count(usage, "output_tokens")
        total = prompt + completion
    else:
        if not set(usage).intersection(
            {
                "promptTokenCount",
                "candidatesTokenCount",
                "thoughtsTokenCount",
                "totalTokenCount",
            }
        ):
            return None
        # Gemini 的 promptTokenCount 已含缓存；thoughts 不在 candidates 中。
        prompt = _count(usage, "promptTokenCount")
        completion = _count(usage, "candidatesTokenCount") + _count(
            usage, "thoughtsTokenCount"
        )
        total = (
            _count(usage, "totalTokenCount")
            if "totalTokenCount" in usage
            else prompt + completion
        )
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total
    )


def _chunk(text=None, usage=None):
    choices = (
        [SimpleNamespace(delta=SimpleNamespace(content=text))]
        if text is not None
        else []
    )
    return SimpleNamespace(choices=choices, usage=usage)


def _sse_lines(response):
    pending, received = bytearray(), 0
    for chunk in response.iter_bytes():
        received += len(chunk)
        if received > _MAX_RESPONSE_BYTES:
            raise IncompleteModelResponse()
        # 不让 HTTPX 按固定块预缓冲，否则短回答要等整个流结束才显示首字。
        for position in range(0, len(chunk), 65536):
            pending.extend(chunk[position : position + 65536])
            while pending:
                positions = [
                    value
                    for value in (pending.find(b"\n"), pending.find(b"\r"))
                    if value >= 0
                ]
                if not positions:
                    break
                end = min(positions)
                if pending[end] == 13 and end + 1 == len(pending):
                    break
                if end > _MAX_EVENT_BYTES:
                    raise IncompleteModelResponse()
                consumed = end + (2 if pending[end : end + 2] == b"\r\n" else 1)
                line = bytes(pending[:end]).decode("utf-8")
                del pending[:consumed]
                yield line
            if len(pending) > _MAX_EVENT_BYTES:
                raise IncompleteModelResponse()
    if pending:
        yield bytes(pending).rstrip(b"\r").decode("utf-8")


def _sse_events(response):
    event, data, size = "", [], 0
    first_line = True
    for line in _sse_lines(response):
        if first_line:
            line = line.removeprefix("\ufeff")
            first_line = False
        if not line:
            if data:
                raw = "\n".join(data)
                if raw == "[DONE]":
                    return
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise IncompleteModelResponse()
                yield event, value
            event, data, size = "", [], 0
        elif line.startswith("data:"):
            value = line[5:].lstrip(" ")
            size += len(value.encode("utf-8"))
            if size > _MAX_EVENT_BYTES:
                raise IncompleteModelResponse()
            data.append(value)
        elif line.startswith("event:"):
            event = line[6:].strip()
    if data:
        raw = "\n".join(data)
        if raw != "[DONE]":
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise IncompleteModelResponse()
            yield event, value


def _anthropic_text(blocks):
    if not isinstance(blocks, list):
        raise IncompleteModelResponse()
    texts = []
    for block in blocks:
        if not isinstance(block, dict):
            raise IncompleteModelResponse()
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif kind not in ("thinking", "redacted_thinking"):
            raise IncompleteModelResponse()
    return "".join(texts)


def _gemini_candidate(payload, *, allow_no_candidate=False):
    feedback = payload.get("promptFeedback", {})
    if not isinstance(feedback, dict) or feedback.get("blockReason") not in (
        None,
        "",
        "BLOCK_REASON_UNSPECIFIED",
    ):
        raise IncompleteModelResponse()
    candidates = payload.get("candidates", [])
    if allow_no_candidate and candidates == []:
        return "", None
    if (
        not isinstance(candidates, list)
        or len(candidates) != 1
        or not isinstance(candidates[0], dict)
    ):
        raise IncompleteModelResponse()
    candidate = candidates[0]
    if candidate.get("index") not in (None, 0):
        raise IncompleteModelResponse()
    finish = candidate.get("finishReason")
    if finish not in (None, "", "STOP"):
        raise IncompleteModelResponse()
    content = candidate.get("content", {})
    if not isinstance(content, dict) or content.get("role") not in (None, "model"):
        raise IncompleteModelResponse()
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        raise IncompleteModelResponse()
    texts = []
    for part in parts:
        if not isinstance(part, dict):
            raise IncompleteModelResponse()
        if "thought" in part and type(part["thought"]) is not bool:
            raise IncompleteModelResponse()
        if set(part).intersection(
            {
                "functionCall",
                "functionResponse",
                "inlineData",
                "fileData",
                "executableCode",
                "codeExecutionResult",
            }
        ):
            raise IncompleteModelResponse()
        if part.get("thought") is True:
            continue
        if not isinstance(part.get("text"), str):
            raise IncompleteModelResponse()
        texts.append(part["text"])
    return "".join(texts), finish


class NativeModelClient:
    def __init__(self, *, protocol, api_key, base_url, http_client=None, timeout=60.0):
        if (
            protocol not in ("anthropic", "gemini")
            or not isinstance(api_key, str)
            or not api_key
        ):
            raise ModelProviderError(400)
        from core.provider_transport import build_provider_http_client

        self._protocol = protocol
        self._base_url = base_url.rstrip("/")
        self._http = (
            http_client
            if http_client is not None
            else build_provider_http_client(base_url)
        )
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, 10.0))
        self._headers = (
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
            if protocol == "anthropic"
            else {"x-goog-api-key": api_key}
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.embeddings = SimpleNamespace(create=self._embed)

    def _json(self, url, payload):
        try:
            with self._http.stream(
                "POST", url, headers=self._headers, json=payload, timeout=self._timeout
            ) as response:
                if response.status_code >= 400:
                    raise ModelProviderError(response.status_code)
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise IncompleteModelResponse()
                    body.extend(chunk)
                value = json.loads(body)
                if not isinstance(value, dict) or "error" in value:
                    raise IncompleteModelResponse()
                return value
        except ModelProviderError:
            raise
        except (ValueError, TypeError):
            raise IncompleteModelResponse() from None
        except Exception:
            raise ModelProviderError() from None

    def _create(
        self,
        *,
        model,
        messages,
        stream=False,
        stream_options=None,
        max_tokens=None,
        temperature=None,
        top_p=None,
        frequency_penalty=None,
        **unsupported,
    ):
        if unsupported or type(stream) is not bool:
            raise ModelProviderError(400)
        if stream_options is not None and (
            not isinstance(stream_options, dict)
            or set(stream_options) - {"include_usage"}
            or type(stream_options.get("include_usage", True)) is not bool
        ):
            raise ModelProviderError(400)
        if max_tokens is not None and (type(max_tokens) is not int or max_tokens < 1):
            raise ModelProviderError(400)
        system, turns = _messages(messages)
        if self._protocol == "anthropic":
            if not isinstance(model, str) or not _ANTHROPIC_MODEL.fullmatch(model):
                raise ModelProviderError(400)
            payload = {
                "model": model,
                "messages": turns,
                "max_tokens": max_tokens or 4096,
                "stream": stream,
            }
            if system:
                payload["system"] = system
            # Claude 4.7+ 即旧 API 版也拒绝非默认采样值，不能转发现有 OpenAI 参数。
            url = self._base_url + "/messages"
        else:
            match = _GEMINI_MODEL.fullmatch(model) if isinstance(model, str) else None
            if not match:
                raise ModelProviderError(400)
            payload = {
                "contents": [
                    {
                        "role": "model" if turn["role"] == "assistant" else "user",
                        "parts": [{"text": turn["content"]}],
                    }
                    for turn in turns
                ]
            }
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}
            generation = {"candidateCount": 1}
            for name, value in (
                ("temperature", temperature),
                ("topP", top_p),
                ("frequencyPenalty", frequency_penalty),
            ):
                if value is not None:
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ModelProviderError(400)
                    generation[name] = value
            if max_tokens is not None:
                generation["maxOutputTokens"] = max_tokens
            payload["generationConfig"] = generation
            action = "streamGenerateContent?alt=sse" if stream else "generateContent"
            url = self._base_url + "/models/" + match.group(1) + ":" + action
        if stream:
            return self._stream(url, payload)
        result = self._json(url, payload)
        if self._protocol == "anthropic":
            if result.get("stop_reason") not in ("end_turn", "stop_sequence"):
                raise IncompleteModelResponse()
            text = _anthropic_text(result.get("content"))
            usage = _usage(result.get("usage"), "anthropic")
        else:
            text, finish = _gemini_candidate(result)
            if finish != "STOP":
                raise IncompleteModelResponse()
            usage = _usage(result.get("usageMetadata"), "gemini")
        if not text.strip():
            raise IncompleteModelResponse()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=usage,
        )

    def _stream(self, url, payload):
        # 连接在首次迭代时才打开；从未迭代的流 close() 不会留下悬空 HTTP 响应。
        try:
            with self._http.stream(
                "POST", url, headers=self._headers, json=payload, timeout=self._timeout
            ) as response:
                if response.status_code >= 400:
                    raise ModelProviderError(response.status_code)
                events = _sse_events(response)
                if self._protocol == "anthropic":
                    yield from self._anthropic_stream(events)
                else:
                    yield from self._gemini_stream(events)
        except ModelProviderError:
            raise
        except Exception:
            raise IncompleteModelResponse() from None

    def _anthropic_stream(self, events):
        started, stopped, visible = False, False, False
        finish, usage = None, {}
        for event, value in events:
            kind = value.get("type", event)
            if event == "error" or kind == "error" or "error" in value:
                raise IncompleteModelResponse()
            if event and value.get("type") is not None and event != value["type"]:
                raise IncompleteModelResponse()
            if kind == "message_start":
                if started:
                    raise IncompleteModelResponse()
                started = True
                message = value.get("message", {})
                if not isinstance(message, dict):
                    raise IncompleteModelResponse()
                if message.get("usage") is not None:
                    usage.update(message["usage"])
            elif kind == "content_block_start":
                if not started:
                    raise IncompleteModelResponse()
                block = value.get("content_block", {})
                text = _anthropic_text([block])
                if text:
                    visible = visible or bool(text.strip())
                    yield _chunk(text)
            elif kind == "content_block_delta":
                if not started:
                    raise IncompleteModelResponse()
                delta = value.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if not isinstance(text, str):
                        raise IncompleteModelResponse()
                    visible = visible or bool(text.strip())
                    if text:
                        yield _chunk(text)
                elif delta.get("type") not in ("thinking_delta", "signature_delta"):
                    raise IncompleteModelResponse()
            elif kind == "message_delta":
                if not started:
                    raise IncompleteModelResponse()
                finish = value.get("delta", {}).get("stop_reason", finish)
                if finish not in (None, "end_turn", "stop_sequence"):
                    raise IncompleteModelResponse()
                if value.get("usage") is not None:
                    usage.update(value["usage"])
            elif kind == "message_stop":
                stopped = True
                break
        if (
            not started
            or not stopped
            or not visible
            or finish not in ("end_turn", "stop_sequence")
        ):
            raise IncompleteModelResponse()
        yield _chunk(usage=_usage(usage, "anthropic"))

    def _gemini_stream(self, events):
        visible, finished, usage = False, False, {}
        for event, value in events:
            if event == "error" or "error" in value:
                raise IncompleteModelResponse()
            text, finish = _gemini_candidate(value, allow_no_candidate=True)
            if text:
                if finished:
                    raise IncompleteModelResponse()
                visible = visible or bool(text.strip())
                yield _chunk(text)
            if finish == "STOP":
                finished = True
            if value.get("usageMetadata") is not None:
                usage.update(value["usageMetadata"])
        if not visible or not finished:
            raise IncompleteModelResponse()
        yield _chunk(usage=_usage(usage, "gemini"))

    def _embed(self, *, model, input, **unsupported):
        match = _GEMINI_MODEL.fullmatch(model) if isinstance(model, str) else None
        if self._protocol != "gemini" or unsupported or not match:
            raise ModelProviderError(400)
        texts = [input] if isinstance(input, str) else input
        if (
            not isinstance(texts, list)
            or not texts
            or any(not isinstance(text, str) or not text.strip() for text in texts)
        ):
            raise ModelProviderError(400)
        model_name = "models/" + match.group(1)
        payload = {
            "requests": [
                {
                    "model": model_name,
                    "content": {"parts": [{"text": text}]},
                    "embedContentConfig": {
                        "outputDimensionality": GEMINI_EMBEDDING_DIMENSION
                    },
                }
                for text in texts
            ]
        }
        result = self._json(
            self._base_url + "/" + model_name + ":batchEmbedContents", payload
        )
        embeddings = result.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise IncompleteModelResponse()
        vectors = []
        for embedding in embeddings:
            values = embedding.get("values") if isinstance(embedding, dict) else None
            if (
                not isinstance(values, list)
                or len(values) != GEMINI_EMBEDDING_DIMENSION
            ):
                raise IncompleteModelResponse()
            try:
                if any(
                    type(value) not in (int, float) or not math.isfinite(value)
                    for value in values
                ):
                    raise IncompleteModelResponse()
                scale = max(abs(value) for value in values)
                if not scale:
                    raise IncompleteModelResponse()
                scaled = [value / scale for value in values]
                norm = math.sqrt(math.fsum(value * value for value in scaled))
                vectors.append(
                    SimpleNamespace(embedding=[value / norm for value in scaled])
                )
            except (TypeError, ValueError, OverflowError):
                raise IncompleteModelResponse() from None
        return SimpleNamespace(data=vectors)

    def close(self):
        self._http.close()
