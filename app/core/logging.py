"""Structured (JSON lines) logging shared by the API and the worker."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has; anything else was passed through `extra=`.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}
# Safety net: credentials embedded in URLs (scheme://user:password@host).
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


def redact(value: str) -> str:
    """Mask credentials embedded in URLs (`scheme://user:pass@` -> `scheme://***@`).

    Milestone 7: structured logs must never leak secrets.
    """
    return _URL_CREDENTIALS.sub(r"\g<scheme>***@", value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        """Render a record as one JSON line; `extra=` fields become top-level keys.

        Milestone 7: structured logs.
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
        }
        message = record.getMessage()
        if message != payload["event"]:
            payload["message"] = message
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key != "event" and value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO") -> None:
    """Send all logs (app, Celery, SQLAlchemy...) to stdout as JSON lines.

    Milestone 7: called by the API (`app/main.py`) and the worker (Celery `setup_logging`).
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Log a named event with structured fields, e.g. order_id=..., attempt=...

    Milestone 7: the single logging entry point used across the codebase.
    """
    # stacklevel=2: report the caller's file/line, not this helper's.
    logger.log(level, event, exc_info=exc_info, stacklevel=2, extra={"event": event, **fields})
