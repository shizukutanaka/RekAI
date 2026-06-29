#!/usr/bin/env python3
"""A full tool/function-calling round-trip through RekAI, using only the stdlib.

The same OpenAI-style `tools` work across OpenAI, Anthropic, and Gemini — RekAI
translates them per provider. Needs a tool-capable model, so set a real key:

    REKAI_API_URL=http://localhost:8000 \
    MODEL=gpt-4o-mini REKAI_PROVIDER_KEY=sk-... \
    python python/tools.py

(Other examples: MODEL=claude-sonnet-4-6 or MODEL=gemini-1.5-pro.)
"""

from __future__ import annotations

import json
import os
import urllib.request

API_URL = os.environ.get("REKAI_API_URL", "http://localhost:8000")
MODEL = os.environ.get("MODEL", "gpt-4o-mini")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def get_weather(city: str) -> str:
    """A toy local tool implementation."""
    return f"It's 21°C and sunny in {city}."


def chat(messages: list[dict], tools=None) -> dict:
    body = {"model": MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    headers = {"Content-Type": "application/json"}
    key = os.environ.get("REKAI_PROVIDER_KEY")
    if key:
        headers["X-Provider-Key"] = key
    req = urllib.request.Request(f"{API_URL}/v1/chat", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main() -> None:
    messages = [{"role": "user", "content": "What's the weather in Tokyo?"}]

    # 1) Ask the model — it should request the tool.
    first = chat(messages, tools=TOOLS)
    tool_calls = first.get("tool_calls")
    if not tool_calls:
        print("Model answered directly:", first["content"])
        return

    # 2) Run the requested tools locally and append the results.
    messages.append({"role": "assistant", "content": first["content"], "tool_calls": tool_calls})
    for call in tool_calls:
        args = json.loads(call["function"]["arguments"] or "{}")
        result = get_weather(**args)
        print(f"→ {call['function']['name']}({args}) = {result}")
        messages.append(
            {"role": "tool", "tool_call_id": call["id"], "name": call["function"]["name"], "content": result}
        )

    # 3) Send the tool results back for a final natural-language answer.
    final = chat(messages)
    print("\nAssistant:", final["content"])


if __name__ == "__main__":
    main()
