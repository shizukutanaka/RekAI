"""Tests for the Gemini provider, with the HTTP layer mocked."""

from __future__ import annotations

import httpx
import pytest

from rekai.providers.base import ProviderError
from rekai.providers.gemini import GeminiProvider
from rekai.schemas import ChatMessage, ChatRequest


def _req(**kwargs) -> ChatRequest:
    kwargs.setdefault("model", "gemini-1.5-flash")
    kwargs.setdefault("messages", [ChatMessage(role="user", content="hi")])
    return ChatRequest(**kwargs)


async def test_requires_key() -> None:
    with pytest.raises(ProviderError) as exc:
        await GeminiProvider().chat(_req(), api_key=None)
    assert exc.value.status_code == 401


async def test_chat_parses_response(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "candidates": [{"content": {"parts": [{"text": "Hello "}, {"text": "world"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 3,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 5,
                },
            }

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    msgs = [
        ChatMessage(role="system", content="be terse"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="prev"),
    ]
    result = await GeminiProvider().chat(_req(messages=msgs, max_tokens=32), api_key="g-key")

    assert result.content == "Hello world"
    assert result.usage.total_tokens == 5
    # system prompt hoisted, assistant mapped to role "model".
    assert captured["json"]["systemInstruction"]["parts"][0]["text"] == "be terse"
    roles = [c["role"] for c in captured["json"]["contents"]]
    assert roles == ["user", "model"]
    assert captured["json"]["generationConfig"]["maxOutputTokens"] == 32
    assert captured["headers"]["x-goog-api-key"] == "g-key"
    assert ":generateContent" in captured["url"]


async def test_chat_propagates_http_error(monkeypatch) -> None:
    class FakeResponse:
        status_code = 429
        text = "rate limited"
        headers = {"Retry-After": "12"}

        def json(self) -> dict:  # pragma: no cover
            return {}

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    with pytest.raises(ProviderError) as exc:
        await GeminiProvider().chat(_req(), api_key="g-key")
    assert exc.value.status_code == 429
    assert exc.value.retry_after == 12.0  # captured from the upstream header


# --- model id → URL path safety ------------------------------------------------


async def test_chat_rejects_model_ids_with_path_chars() -> None:
    # The model id is spliced into the upstream URL path; a "/" or "?" would
    # traverse or inject into it (still carrying the operator's key), so it is
    # rejected before the URL is built.
    for bad in (
        "gemini-x/../v1beta2/models",
        "gemini-x?key=1",
        "gemini-x#y",
        "gemini-x&alt=html",
        "gemini-x%2f..%2f",
        "gemini x",
    ):
        with pytest.raises(ProviderError) as exc:
            await GeminiProvider().chat(_req(model=bad), api_key="g-key")
        assert exc.value.status_code == 400


async def test_embed_rejects_model_ids_with_path_chars() -> None:
    with pytest.raises(ProviderError) as exc:
        await GeminiProvider().embed(["hi"], "text-embedding-004/../x", api_key="g-key")
    assert exc.value.status_code == 400


async def test_stream_rejects_model_ids_with_path_chars() -> None:
    with pytest.raises(ProviderError) as exc:
        async for _ in GeminiProvider().stream_events(_req(model="gemini-x/../"), api_key="g-key"):
            pass
    assert exc.value.status_code == 400


async def test_models_prefixed_id_normalizes_url(monkeypatch) -> None:
    # A qualified "models/<id>" is accepted on all three call paths, not just
    # embed — otherwise chat produced "models/models/<id>" and 404'd.
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    await GeminiProvider().chat(_req(model="models/gemini-1.5-flash"), api_key="g-key")
    assert "/models/gemini-1.5-flash:generateContent" in captured["url"]
    assert "models/models/" not in captured["url"]


# --- streaming ---------------------------------------------------------------


class _GeminiStreamClient:
    """Replays a Gemini SSE sequence: text deltas, a functionCall chunk, a
    malformed line, then a terminal chunk with usageMetadata."""

    captured: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def aclose(self) -> None:
        return None

    def stream(self, method, url, json=None, headers=None):
        _GeminiStreamClient.captured = {"url": url, "json": json}

        class _Resp:
            status_code = 200

            async def aiter_lines(self):
                yield 'data: {"candidates":[{"content":{"parts":[{"text":"Hello "}]}}]}'
                yield (
                    'data: {"candidates":[{"content":{"parts":'
                    '[{"functionCall":{"name":"lookup","args":{"id":1}}}]}}]}'
                )
                yield "data: not-json"
                yield (
                    'data: {"candidates":[{"content":{"parts":[{"text":"world"}]},'
                    '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":5,'
                    '"candidatesTokenCount":3,"totalTokenCount":8}}'
                )

            async def aread(self) -> bytes:
                return b""

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


async def test_gemini_stream_events_yields_deltas_usage_and_tool_calls(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _GeminiStreamClient)
    events = [ev async for ev in GeminiProvider().stream_events(_req(), "gk")]
    deltas = [e.delta for e in events if e.delta]
    assert deltas == ["Hello ", "world"]
    usage = next(e.usage for e in events if e.usage)
    assert usage.total_tokens == 8
    calls = next(e.tool_calls for e in events if e.tool_calls)
    assert calls[0]["function"]["name"] == "lookup"
    finish = next(e.finish_reason for e in events if e.finish_reason)
    # Gemini STOP with pending function calls normalizes to tool_calls.
    assert finish == "tool_calls"


class _GeminiStreamErrorClient(_GeminiStreamClient):
    def stream(self, method, url, json=None, headers=None):
        class _Resp:
            status_code = 503

            async def aiter_lines(self):
                return
                yield

            async def aread(self) -> bytes:
                return b"upstream exploded"

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


async def test_gemini_stream_error_maps_5xx_to_502(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _GeminiStreamErrorClient)
    with pytest.raises(ProviderError) as exc:
        async for _ in GeminiProvider().stream_events(_req(), "gk"):
            pass
    assert exc.value.status_code == 502
    assert "upstream exploded" in str(exc.value)
