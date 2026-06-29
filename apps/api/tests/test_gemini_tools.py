"""Tests for OpenAI<->Gemini tool-calling translation."""

from __future__ import annotations

import json

import httpx
import pytest

from rekai.providers.gemini import (
    GeminiProvider,
    _extract_tool_calls,
    _translate_contents,
    _translate_tool_config,
    _translate_tools,
)
from rekai.schemas import ChatMessage, ChatRequest

OPENAI_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def test_translate_tools() -> None:
    out = _translate_tools([OPENAI_TOOL])
    assert out == [
        {
            "functionDeclarations": [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ]
        }
    ]


@pytest.mark.parametrize(
    "choice,mode,allowed",
    [
        ("auto", "AUTO", None),
        ("required", "ANY", None),
        ("none", "NONE", None),
        ({"type": "function", "function": {"name": "get_weather"}}, "ANY", ["get_weather"]),
    ],
)
def test_translate_tool_config(choice, mode, allowed) -> None:
    cfg = _translate_tool_config(choice)["functionCallingConfig"]
    assert cfg["mode"] == mode
    assert cfg.get("allowedFunctionNames") == allowed


def test_translate_tool_config_none() -> None:
    assert _translate_tool_config(None) is None


def test_translate_contents_round_trip() -> None:
    msgs = [
        ChatMessage(role="user", content="weather?"),
        ChatMessage(
            role="assistant",
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                }
            ],
        ),
        ChatMessage(role="tool", name="get_weather", content="sunny"),
    ]
    out = _translate_contents(msgs)
    assert out[1]["role"] == "model"
    assert out[1]["parts"][0]["functionCall"]["args"] == {"city": "Tokyo"}
    assert out[2]["parts"][0]["functionResponse"]["name"] == "get_weather"


def test_extract_tool_calls() -> None:
    data = {
        "candidates": [
            {
                "content": {
                    "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}}}]
                }
            }
        ]
    }
    calls = _extract_tool_calls(data)
    assert calls is not None and len(calls) == 1
    assert calls[0]["function"]["name"] == "get_weather"
    assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Tokyo"}
    assert _extract_tool_calls({"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}) is None


async def test_gemini_chat_forwards_and_returns_tool_calls(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "get_weather", "args": {"city": "Tokyo"}}}
                            ]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 4,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 6,
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
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    req = ChatRequest(
        model="gemini-1.5-pro",
        messages=[ChatMessage(role="user", content="weather in Tokyo?")],
        tools=[OPENAI_TOOL],
        tool_choice="auto",
    )
    result = await GeminiProvider().chat(req, api_key="g-key")

    assert captured["payload"]["tools"][0]["functionDeclarations"][0]["name"] == "get_weather"
    assert captured["payload"]["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    assert result.tool_calls is not None
    assert result.tool_calls[0]["function"]["name"] == "get_weather"
