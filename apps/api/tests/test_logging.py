"""Tests for the logging configuration (text vs structured JSON)."""

from __future__ import annotations

import json
import logging

from rekai.logging_config import JsonFormatter, configure_logging


def test_json_formatter_emits_one_object() -> None:
    record = logging.LogRecord(
        name="rekai.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    line = JsonFormatter().format(record)
    obj = json.loads(line)
    assert obj["level"] == "INFO"
    assert obj["logger"] == "rekai.access"
    assert obj["message"] == "hello world"
    assert "ts" in obj


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.LogRecord(
        name="rekai.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="GET /v1/chat",
        args=(),
        exc_info=None,
    )
    record.method = "GET"
    record.status = 200
    record.duration_ms = 1.2
    obj = json.loads(JsonFormatter().format(record))
    assert obj["method"] == "GET"
    assert obj["status"] == 200
    assert obj["duration_ms"] == 1.2


def test_configure_logging_json_format() -> None:
    configure_logging("INFO", "json")
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, JsonFormatter)
    # Restore the default text formatter for other tests.
    configure_logging("INFO", "text")
    assert not isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)


def test_access_log_carries_gen_ai_attributes(client, caplog) -> None:
    """A chat request's access-log record carries OTel GenAI attributes."""
    with caplog.at_level(logging.INFO, logger="rekai.access"):
        client.post(
            "/v1/chat",
            json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
        )
    rec = next(
        r
        for r in caplog.records
        if r.name == "rekai.access" and getattr(r, "path", "") == "/v1/chat"
    )
    assert getattr(rec, "gen_ai.operation.name") == "chat"
    assert getattr(rec, "gen_ai.provider.name") == "echo"
    assert getattr(rec, "gen_ai.request.model") == "echo"
    assert getattr(rec, "gen_ai.usage.output_tokens") > 0


def test_access_log_gen_ai_operation_for_embeddings(client, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="rekai.access"):
        client.post("/v1/embeddings", json={"model": "echo", "input": "hello"})
    rec = next(
        r
        for r in caplog.records
        if r.name == "rekai.access" and getattr(r, "path", "") == "/v1/embeddings"
    )
    assert getattr(rec, "gen_ai.operation.name") == "embeddings"
    assert getattr(rec, "gen_ai.provider.name") == "echo"
