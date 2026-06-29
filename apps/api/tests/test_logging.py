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
