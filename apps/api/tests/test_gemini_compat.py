"""Tests for the Gemini-compatible /v1beta/models/{model}:generateContent surface."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app


def _body(**over) -> dict:
    body = {
        "contents": [{"role": "user", "parts": [{"text": "hello world"}]}],
    }
    body.update(over)
    return body


def _parse_sse(text: str) -> list[dict]:
    return [
        json.loads(line[len("data:") :]) for line in text.splitlines() if line.startswith("data:")
    ]


def test_generate_content_shape(client: TestClient) -> None:
    resp = client.post("/v1beta/models/echo:generateContent", json=_body())
    assert resp.status_code == 200
    body = resp.json()
    cand = body["candidates"][0]
    assert cand["content"]["role"] == "model"
    assert cand["content"]["parts"][0]["text"] == "Echo: hello world"
    assert cand["finishReason"] == "STOP"
    assert cand["index"] == 0
    usage = body["usageMetadata"]
    assert usage["promptTokenCount"] > 0
    assert usage["candidatesTokenCount"] > 0
    assert usage["totalTokenCount"] == (usage["promptTokenCount"] + usage["candidatesTokenCount"])
    assert body["rekaiProvider"] == "echo"


def test_model_in_path_routes(client: TestClient) -> None:
    resp = client.post("/v1beta/models/echo:generateContent", json=_body())
    assert resp.status_code == 200
    assert resp.json()["modelVersion"] == "echo"


def test_system_instruction_maps_to_system(client: TestClient) -> None:
    resp = client.post(
        "/v1beta/models/echo:generateContent",
        json=_body(systemInstruction={"parts": [{"text": "Be terse"}]}),
    )
    assert resp.status_code == 200


def test_generation_config_fields(client: TestClient) -> None:
    resp = client.post(
        "/v1beta/models/echo:generateContent",
        json=_body(
            generationConfig={
                "temperature": 0.2,
                "maxOutputTokens": 16,
                "stopSequences": ["\n\n"],
                "topP": 0.9,
            }
        ),
    )
    assert resp.status_code == 200


def test_function_call_round_trip(client: TestClient) -> None:
    resp = client.post(
        "/v1beta/models/echo:generateContent",
        json=_body(
            contents=[
                {"role": "user", "parts": [{"text": "weather?"}]},
                {
                    "role": "model",
                    "parts": [{"functionCall": {"name": "get_weather", "args": {"c": "Tokyo"}}}],
                },
                {
                    "role": "user",
                    "parts": [
                        {"functionResponse": {"name": "get_weather", "response": {"r": "sunny"}}}
                    ],
                },
            ],
            tools=[
                {
                    "functionDeclarations": [
                        {
                            "name": "get_weather",
                            "description": "Look up weather",
                            "parameters": {"type": "object"},
                        }
                    ]
                }
            ],
            toolConfig={"functionCallingConfig": {"mode": "ANY"}},
        ),
    )
    assert resp.status_code == 200


def test_unsupported_part_is_a_readable_400(client: TestClient) -> None:
    resp = client.post(
        "/v1beta/models/echo:generateContent",
        json=_body(
            contents=[
                {
                    "role": "user",
                    "parts": [{"inline_data": {"mime_type": "image/png", "data": "…"}}],
                }
            ]
        ),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["status"] == "INVALID_ARGUMENT"
    assert "Unsupported part" in body["error"]["message"]


def test_missing_contents_422_in_google_envelope(client: TestClient) -> None:
    resp = client.post("/v1beta/models/echo:generateContent", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["status"] == "INVALID_ARGUMENT"
    assert body["error"]["code"] == 422


def test_x_goog_api_key_is_the_gateway_credential() -> None:
    app = create_app(
        Settings(
            environment="test",
            default_provider="echo",
            api_keys="sk-good",
            cache_enabled=True,
        )
    )
    with TestClient(app) as c:
        ok = c.post(
            "/v1beta/models/echo:generateContent",
            json=_body(),
            headers={"x-goog-api-key": "sk-good"},
        )
        assert ok.status_code == 200
        bad = c.post(
            "/v1beta/models/echo:generateContent",
            json=_body(),
            headers={"x-goog-api-key": "sk-bad"},
        )
        assert bad.status_code == 401
        body = bad.json()
        assert body["error"]["status"] == "UNAUTHENTICATED"


def test_rate_limit_uses_google_envelope() -> None:
    app = create_app(
        Settings(
            environment="test",
            default_provider="echo",
            rate_limit_enabled=True,
            rate_limit_requests=1,
            cache_enabled=False,
        )
    )
    with TestClient(app) as c:
        assert c.post("/v1beta/models/echo:generateContent", json=_body()).status_code == 200
        second = c.post("/v1beta/models/echo:generateContent", json=_body())
        assert second.status_code == 429
        assert second.json()["error"]["status"] == "RESOURCE_EXHAUSTED"


def test_stream_generate_content(client: TestClient) -> None:
    resp = client.post(
        "/v1beta/models/echo:streamGenerateContent?alt=sse",
        json=_body(),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    text = "".join(
        part["text"]
        for e in events
        for c in e.get("candidates", [])
        for part in c.get("content", {}).get("parts", [])
        if "text" in part
    )
    assert text == "Echo: hello world"
    last = events[-1]
    assert last["candidates"][0]["finishReason"] == "STOP"
    assert last["usageMetadata"]["totalTokenCount"] > 0
