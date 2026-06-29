"""Logging setup — human-readable text by default, or structured JSON."""

from __future__ import annotations

import json
import logging
import sys

_TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s :: %(message)s"

# Attributes present on every LogRecord; anything else was passed via `extra=`
# and is worth emitting as a structured field.
_RESERVED = set(logging.makeLogRecord({}).__dict__.keys() | {"message", "asctime", "taskName"})


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line, including any `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    if fmt.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
