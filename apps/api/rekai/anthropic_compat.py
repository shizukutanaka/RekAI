"""Translation between the Anthropic Messages shape and RekAI's internal chat
schema — the ``/v1/messages`` counterpart of ``openai_compat``.

Pure functions only — no I/O. The route in ``main.py`` translates the request
here, runs it through the same internal pipeline as ``/v1/chat`` (routing,
cache, retries, fallback, budgets, metrics all apply), and translates the
result back. This keeps RekAI a drop-in ``base_url`` for the Anthropic SDKs
without duplicating orchestration.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from rekai.pricing import estimate_tokens
from rekai.providers.base import ProviderError
from rekai.schemas import (
    AnthropicContentBlock,
    AnthropicCountTokensRequest,
    AnthropicMessagesRequest,
    AnthropicTool,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Usage,
)

# Anthropic uses a different type vocabulary than OpenAI for the same ideas;
# the maps below keep the translation honest in both directions.

# Internal finish_reason -> Anthropic stop_reason.
_FINISH_TO_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def _flatten_system(system: str | list[dict[str, Any]] | None) -> str | None:
    """Anthropic's ``system`` is a string or a list of text blocks."""
    if system is None or isinstance(system, str):
        return system
    texts = [
        block.get("text", "")
        for block in system
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(t for t in texts if t) or None


def _flatten_content(
    content: str | list[AnthropicContentBlock], role: str
) -> tuple[str | None, list[dict], list[ChatMessage], list[dict], list[dict]]:
    """Reduce a Messages-API content array to (text, tool_calls, tool_messages,
    thinking_blocks, extra_blocks).

    - ``text`` blocks join into the message's plain text.
    - ``tool_use`` blocks on an assistant turn become OpenAI-shaped tool_calls
      (the internal interchange format — providers translate them natively).
    - ``tool_result`` blocks become trailing ``role="tool"`` messages, matching
      the OpenAI convention the pipeline already speaks. A tool_result's own
      content (string or block list) is flattened to text the same way.
    - ``thinking``/``redacted_thinking`` blocks on an assistant turn are kept
      verbatim — Anthropic requires them echoed back in multi-turn thinking
      conversations (the signature proves the text wasn't tampered with).
    - every other block type on an assistant turn (server_tool_use,
      web_search_tool_result, mcp_*, code_execution, ...) is kept verbatim in
      ``extra_blocks`` — Anthropic requires the server-tool trace echoed back
      in multi-turn context, and unknown types stay forward-compatible.
    - on a user turn anything else (image/document blocks) is a readable 400
      rather than a silent drop — same rule the OpenAI layer applies to
      non-text parts.
    """
    if isinstance(content, str):
        return content, [], [], [], []
    texts: list[str] = []
    tool_calls: list[dict] = []
    tool_messages: list[ChatMessage] = []
    thinking_blocks: list[dict] = []
    extra_blocks: list[dict] = []
    for block in content:
        if block.type == "text":
            if block.text is not None:
                texts.append(block.text)
        elif block.type in ("thinking", "redacted_thinking"):
            if role != "assistant":
                raise ProviderError(
                    "'thinking' blocks belong on assistant messages.",
                    status_code=400,
                )
            thinking_blocks.append(block.model_dump(exclude_none=True))
        elif block.type == "tool_use":
            if role != "assistant":
                raise ProviderError(
                    "'tool_use' blocks belong on assistant messages.",
                    status_code=400,
                )
            tool_calls.append(
                {
                    "id": block.id or f"toolu_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input or {}),
                    },
                }
            )
        elif block.type == "tool_result":
            body = block.content
            if isinstance(body, list):
                body = "\n".join(
                    b.get("text", "")
                    for b in body
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            tool_messages.append(
                ChatMessage(
                    role="tool",
                    content=body or "",
                    tool_call_id=block.tool_use_id,
                )
            )
        elif role == "assistant":
            extra_blocks.append(block.model_dump(exclude_none=True))
        else:
            raise ProviderError(
                f"Unsupported content block type '{block.type}'; "
                "RekAI accepts text, tool_use, tool_result, and (on assistant "
                "turns) thinking and server-tool blocks.",
                status_code=400,
            )
    return (
        "\n".join(texts) if texts else None,
        tool_calls,
        tool_messages,
        thinking_blocks,
        extra_blocks,
    )


def _to_openai_tool(tool: AnthropicTool) -> dict:
    """Anthropic tool -> the OpenAI function shape providers already speak."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _to_openai_tool_choice(choice) -> Any:
    """Anthropic {type: auto|none|any|tool, name?} -> OpenAI tool_choice."""
    if choice.type == "auto":
        return "auto"
    if choice.type == "none":
        return "none"
    if choice.type == "any":
        return "required"
    return {"type": "function", "function": {"name": choice.name}}


def to_chat_request(req: AnthropicMessagesRequest) -> ChatRequest:
    """Translate an Anthropic Messages request into RekAI's ChatRequest."""
    messages: list[ChatMessage] = []
    system = _flatten_system(req.system)
    if system is not None:
        messages.append(ChatMessage(role="system", content=system))
    for m in req.messages:
        text, tool_calls, tool_msgs, thinking, extra = _flatten_content(m.content, m.role)
        messages.append(
            ChatMessage(
                role=m.role,
                content=text,
                tool_calls=tool_calls or None,
                thinking_blocks=thinking or None,
                extra_blocks=extra or None,
            )
        )
        messages.extend(tool_msgs)
    # Anthropic requires temperature=1 when extended thinking is enabled; a
    # caller who left it unset expects Anthropic's own default, not ours.
    thinking_enabled = isinstance(req.thinking, dict) and req.thinking.get("type") == "enabled"
    if req.temperature is not None:
        temperature = req.temperature
    elif thinking_enabled:
        temperature = 1.0
    else:
        temperature = 0.7
    return ChatRequest(
        model=req.model,
        messages=messages,
        provider=req.provider,
        temperature=temperature,
        max_tokens=req.max_tokens,
        stop=req.stop_sequences,
        cache=True,
        tools=[_to_openai_tool(t) for t in req.tools] if req.tools else None,
        tool_choice=_to_openai_tool_choice(req.tool_choice) if req.tool_choice else None,
        thinking=req.thinking,
    )


def _content_blocks(resp: ChatResponse) -> list[dict]:
    blocks: list[dict] = []
    # Thinking blocks lead the content, matching Anthropic's response order.
    if resp.thinking_blocks:
        blocks.extend(resp.thinking_blocks)
    # The server-tool trace (server_tool_use, *_tool_result, mcp_*) sits
    # between thinking and the answer text, matching upstream order.
    if resp.extra_blocks:
        blocks.extend(resp.extra_blocks)
    if resp.content:
        blocks.append({"type": "text", "text": resp.content})
    for tc in resp.tool_calls or []:
        fn = tc.get("function", {})
        try:
            tool_input = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            tool_input = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name"),
                "input": tool_input,
            }
        )
    return blocks or [{"type": "text", "text": ""}]


def to_message(resp: ChatResponse) -> dict:
    """Translate RekAI's ChatResponse into an Anthropic Message object."""
    finish = resp.finish_reason or ("tool_calls" if resp.tool_calls else "stop")
    return {
        "id": f"msg_{resp.id}" if not resp.id.startswith("msg_") else resp.id,
        "type": "message",
        "role": "assistant",
        "content": _content_blocks(resp),
        "model": resp.model,
        "stop_reason": _FINISH_TO_STOP_REASON.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": resp.usage.prompt_tokens,
            "output_tokens": resp.usage.completion_tokens,
        },
        # RekAI observability extras — ignored by the SDK, useful to operators.
        "provider": resp.provider,
        "cost_usd": resp.cost_usd,
        "cached": resp.cached,
    }


# --- streaming event builders (the Messages SSE protocol) ------------------
#
# Anthropic's stream is a typed event sequence, each `event:`/`data:` pair:
# message_start -> content_block_start -> content_block_delta* ->
# content_block_stop -> [tool_use blocks] -> message_delta -> message_stop.


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def ev_message_start(msg_id: str, model: str) -> str:
    return sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )


def ev_content_block_start(index: int, block: dict) -> str:
    return sse(
        "content_block_start",
        {"type": "content_block_start", "index": index, "content_block": block},
    )


def ev_text_delta(index: int, text: str) -> str:
    return sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def ev_thinking_delta(index: int, thinking: str) -> str:
    return sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": thinking},
        },
    )


def ev_signature_delta(index: int, signature: str) -> str:
    return sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": signature},
        },
    )


def ev_block_delta(index: int, delta: dict) -> str:
    """A verbatim content_block_delta for a block RekAI doesn't interpret —
    server-tool blocks stream their own delta vocabulary through untouched."""
    return sse(
        "content_block_delta",
        {"type": "content_block_delta", "index": index, "delta": delta},
    )


def ev_input_json_delta(index: int, partial_json: str) -> str:
    return sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        },
    )


def ev_content_block_stop(index: int) -> str:
    return sse("content_block_stop", {"type": "content_block_stop", "index": index})


def ev_message_delta(stop_reason: str, usage: Usage | None) -> str:
    data: dict[str, Any] = {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
    }
    if usage is not None:
        # Anthropic only defines output_tokens here, but we also report
        # input_tokens — we could not populate them in message_start (the
        # provider had not answered yet) and the SDK just accumulates the dict.
        data["usage"] = {
            "input_tokens": usage.prompt_tokens,
            "output_tokens": usage.completion_tokens,
        }
    return sse("message_delta", data)


def ev_message_stop() -> str:
    return sse("message_stop", {"type": "message_stop"})


def ev_error(status_code: int, message: str) -> str:
    """The streaming error event — Anthropic delivers mid-stream failures as an
    `error` event, not an HTTP error (the SSE stream is already 200)."""
    return sse("error", {"type": "error", "error": _error_obj(status_code, message)})


# --- error envelope --------------------------------------------------------


def _error_type_for_status(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code in (400, 422):
        return "invalid_request_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code == 529:
        return "overloaded_error"
    return "api_error"


def _error_obj(status_code: int, message: str) -> dict:
    return {"type": _error_type_for_status(status_code), "message": message}


def anthropic_error(status_code: int, message: str) -> dict:
    """The Anthropic error envelope, so SDK error handling parses RekAI's."""
    return {"type": "error", "error": _error_obj(status_code, message)}


def count_tokens(req: AnthropicCountTokensRequest) -> int:
    """Local input-token estimate for ``POST /v1/messages/count_tokens``.

    Anthropic's own endpoint returns an exact count from its tokenizer, which
    a self-hosted gateway can't reproduce offline (no vocab downloads — the
    same reason ``pricing.estimate_tokens`` exists). We reuse that script-aware
    estimate over the request's text-bearing parts plus the serialized tool
    schemas, so callers doing pre-flight budget checks get a number that is
    honest about CJK text — not a provider-exact figure.
    """
    parts: list[str] = []
    if isinstance(req.system, str):
        parts.append(req.system)
    elif isinstance(req.system, list):
        parts.extend(
            str(b.get("text", ""))
            for b in req.system
            if isinstance(b, dict) and b.get("type") == "text"
        )
    for message in req.messages:
        if isinstance(message.content, str):
            parts.append(message.content)
            continue
        for block in message.content:
            if block.text:
                parts.append(block.text)
            if block.type == "tool_use":
                parts.append(block.name or "")
                if block.input is not None:
                    parts.append(json.dumps(block.input))
            elif block.type == "tool_result" and block.content is not None:
                parts.append(
                    block.content if isinstance(block.content, str) else json.dumps(block.content)
                )
    if req.tools:
        parts.append(json.dumps([t.model_dump() for t in req.tools]))
    return sum(estimate_tokens(p) for p in parts if p) or 1
