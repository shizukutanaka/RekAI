"""Translation between the Gemini generateContent shape and RekAI's internal
chat schema — the ``/v1beta/models/{model}:generateContent`` counterpart of
``openai_compat`` / ``anthropic_compat``.

Pure functions only — no I/O. The routes in ``main.py`` translate the request
here, run it through the same internal pipeline as ``/v1/chat`` (routing,
cache, retries, fallback, budgets, metrics all apply), and translate the
result back. This keeps RekAI a drop-in ``base_url`` for the Google genai SDKs
without duplicating orchestration.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from rekai.providers.base import ProviderError
from rekai.schemas import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    GeminiGenerateContentRequest,
    Usage,
)

# Internal finish_reason -> Gemini finishReason.
_FINISH_TO_GEMINI = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",  # function calls terminate a turn with STOP upstream
    "content_filter": "SAFETY",
}


def _flatten_parts(parts: list[Any], role: str | None) -> tuple[str | None, list[dict]]:
    """Reduce Gemini ``parts`` to (text, tool_calls).

    - ``text`` parts join into the message's plain text.
    - ``functionCall`` parts on a model turn become OpenAI-shaped tool_calls.
    - ``functionResponse`` parts are folded to their text body so the model
      sees the tool's reply — the OpenAI-style ``role="tool"`` message the
      pipeline speaks only exists inside the SDK surface translation, and a
      functionResponse arrives inline on the next user turn.
    - anything else (inline_data, file_data, thought) is a readable 400 rather
      than a silent drop — same rule as the other compat layers.
    """
    texts: list[str] = []
    tool_calls: list[dict] = []
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            raise ProviderError("'parts' entries must be objects.", status_code=400)
        if "text" in part:
            texts.append(str(part["text"]))
        elif "functionCall" in part:
            if role != "model":
                raise ProviderError(
                    "'functionCall' parts belong on model turns.",
                    status_code=400,
                )
            fc = part["functionCall"]
            tool_calls.append(
                {
                    "id": f"call_{fc.get('name', 'fn')}_{i}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name"),
                        "arguments": json.dumps(fc.get("args") or {}),
                    },
                }
            )
        elif "functionResponse" in part:
            fr = part["functionResponse"]
            body = fr.get("response")
            if isinstance(body, dict):
                body = json.dumps(body)
            texts.append(str(body or ""))
        else:
            raise ProviderError(
                "Unsupported part type in 'parts'; RekAI accepts text, "
                "functionCall, and functionResponse.",
                status_code=400,
            )
    return ("\n".join(texts) if texts else None), tool_calls


def _to_openai_tools(req: GeminiGenerateContentRequest) -> list[dict] | None:
    decls: list[dict] = []
    for group in req.tools or []:
        for fn in group.get("functionDeclarations", []):
            decls.append(
                {
                    "type": "function",
                    "function": {
                        "name": fn.get("name"),
                        "description": fn.get("description"),
                        "parameters": fn.get("parameters") or {},
                    },
                }
            )
    return decls or None


def _to_tool_choice(req: GeminiGenerateContentRequest) -> Any:
    """toolConfig.functionCallingConfig.mode -> OpenAI tool_choice."""
    cfg = (req.toolConfig or {}).get("functionCallingConfig") or {}
    mode = cfg.get("mode")
    allowed = cfg.get("allowedFunctionNames") or []
    if mode == "NONE":
        return "none"
    if mode == "ANY":
        if len(allowed) == 1:
            return {"type": "function", "function": {"name": allowed[0]}}
        return "required"
    return "auto"  # AUTO or unspecified


def to_chat_request(model: str, req: GeminiGenerateContentRequest) -> ChatRequest:
    """Translate a generateContent request into RekAI's ChatRequest."""
    messages: list[ChatMessage] = []
    if req.systemInstruction is not None:
        sys_text, _ = _flatten_parts(req.systemInstruction.get("parts", []), "user")
        if sys_text:
            messages.append(ChatMessage(role="system", content=sys_text))
    for content in req.contents:
        role: Literal["user", "assistant"] = (
            "assistant" if content.get("role") == "model" else "user"
        )
        text, tool_calls = _flatten_parts(content.get("parts", []), content.get("role"))
        messages.append(ChatMessage(role=role, content=text, tool_calls=tool_calls or None))

    gen = req.generationConfig or {}
    max_tokens = gen.get("maxOutputTokens")
    temperature = gen.get("temperature")
    stop = gen.get("stopSequences")
    return ChatRequest(
        model=model,
        messages=messages,
        temperature=temperature if temperature is not None else 0.7,
        max_tokens=max_tokens,
        stop=stop,
        cache=True,
        tools=_to_openai_tools(req),
        tool_choice=_to_tool_choice(req) if req.toolConfig else None,
    )


def _parts(resp: ChatResponse) -> list[dict]:
    parts: list[dict] = []
    if resp.content:
        parts.append({"text": resp.content})
    for tc in resp.tool_calls or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        parts.append({"functionCall": {"name": fn.get("name"), "args": args}})
    return parts or [{"text": ""}]


def to_generate_content_response(resp: ChatResponse) -> dict:
    """Translate RekAI's ChatResponse into a generateContent response."""
    finish = resp.finish_reason or ("tool_calls" if resp.tool_calls else "stop")
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": _parts(resp)},
                "finishReason": _FINISH_TO_GEMINI.get(finish, "STOP"),
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": resp.usage.prompt_tokens,
            "candidatesTokenCount": resp.usage.completion_tokens,
            "totalTokenCount": resp.usage.total_tokens,
        },
        "modelVersion": resp.model,
        # RekAI observability extras — ignored by the SDK, useful to operators.
        "rekaiProvider": resp.provider,
        "rekaiCostUsd": resp.cost_usd,
        "rekaiCached": resp.cached,
    }


def stream_chunk(model: str, text: str) -> str:
    """One text-delta stream event in generateContent's SSE shape."""
    return (
        "data: "
        + json.dumps(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "index": 0}]}
        )
        + "\n\n"
    )


def stream_final(model: str, finish_reason: str, usage: Usage | None) -> str:
    """Terminal chunk: finishReason + usageMetadata, as upstream sends it."""
    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": [{"text": ""}]},
        "index": 0,
        "finishReason": _FINISH_TO_GEMINI.get(finish_reason, "STOP"),
    }
    payload: dict[str, Any] = {"candidates": [candidate]}
    if usage is not None:
        payload["usageMetadata"] = {
            "promptTokenCount": usage.prompt_tokens,
            "candidatesTokenCount": usage.completion_tokens,
            "totalTokenCount": usage.total_tokens,
        }
    return "data: " + json.dumps(payload) + "\n\n"


def stream_error(status_code: int, message: str) -> str:
    """Mid-stream failure as a trailing error object (the stream is already 200)."""
    return "data: " + json.dumps(gemini_error(status_code, message)) + "\n\n"


# --- error envelope ---------------------------------------------------------


def _status_enum(status_code: int) -> str:
    return {
        400: "INVALID_ARGUMENT",
        401: "UNAUTHENTICATED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        409: "ALREADY_EXISTS",
        413: "INVALID_ARGUMENT",
        422: "INVALID_ARGUMENT",
        429: "RESOURCE_EXHAUSTED",
        500: "INTERNAL",
        503: "UNAVAILABLE",
        529: "UNAVAILABLE",
    }.get(status_code, "INTERNAL")


def gemini_error(status_code: int, message: str) -> dict:
    """The Google API error envelope — `{"error": {code, message, status}}`."""
    return {
        "error": {
            "code": status_code,
            "message": message,
            "status": _status_enum(status_code),
        }
    }
