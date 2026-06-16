"""Structured logging with severity discipline and correlation IDs.

PRD §08:
  - DEBUG (dev) / INFO (business events) / WARN (recoverable) / ERROR (failures)
  - No bare ``print``; every line carries a severity and a correlation ID.

The correlation ID is stored in a ``ContextVar`` so it propagates across awaits
without being threaded through every function. Middleware sets it per request;
the WebSocket handler sets it per session.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# Correlation id == trace/request/session id, depending on context.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


class _JsonFormatter(logging.Formatter):
    """Emits one JSON object per line for Kibana/ES ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Allow structured extras: logger.info("msg", extra={"extra_fields": {...}})
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    """Human-friendly formatter for local dev."""

    def format(self, record: logging.LogRecord) -> str:
        cid = get_correlation_id()
        cid_part = f" [{cid}]" if cid else ""
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} "
            f"{record.name}{cid_part}: {record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure the root logger. Idempotent — safe to call from each service's startup."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Replace any existing handlers so re-configuration in tests is clean.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if json_output else _ConsoleFormatter())
    root.addHandler(handler)

    # Tame noisy third-party loggers; surface our own at the configured level.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# --- PII masking (PRD §14: name + account/phone masked in logs) -------------


def mask_name(name: str | None) -> str:
    """"Rahul Sharma" -> "R. S." — enough to correlate, not to identify."""
    if not name:
        return ""
    return " ".join(f"{part[0]}." for part in name.split() if part)


def mask_account(value: str | None) -> str:
    """Mask all but the last 4 chars of an account/phone/id."""
    if not value:
        return ""
    tail = value[-4:]
    return f"{'*' * max(0, len(value) - 4)}{tail}"

