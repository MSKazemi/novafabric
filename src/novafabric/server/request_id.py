"""Request-ID correlation + structured JSON logging for the server (P1 operability).

An ops team needs to trace one request across the access log, audit events, and
SIEM egress. This module provides:

- a ``request_id`` ContextVar set by :func:`install_request_id_middleware` from
  an inbound ``X-Request-ID`` (sanitised) or a freshly generated id, echoed back
  on the response and available to every log record via :class:`RequestIdFilter`;
- :class:`JsonLogFormatter` + :func:`configure_logging` so ``nova server start
  --log-format json`` (or ``NOVAFABRIC_SERVER_LOG_FORMAT=json``) emits
  machine-parseable logs that carry the request id.

Nothing here changes request handling; it is observability only.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

REQUEST_ID_HEADER = "X-Request-ID"
LOG_FORMAT_ENV = "NOVAFABRIC_SERVER_LOG_FORMAT"

#: Per-request correlation id; empty string when outside a request.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Accept only short, safe inbound ids (defends the logs against injection /
# unbounded values); anything else is replaced with a generated id.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def new_request_id() -> str:
    """Generate a fresh request id (uuid4 hex)."""
    return uuid.uuid4().hex


def sanitize_request_id(value: str | None) -> str:
    """Return a safe inbound id, or a freshly generated one when absent/invalid."""
    if value and _SAFE_ID.match(value):
        return value
    return new_request_id()


def current_request_id() -> str:
    """The active request id, or empty string outside a request context."""
    return request_id_var.get()


def install_request_id_middleware(app: "FastAPI") -> None:
    """Set the request-id ContextVar per request and echo the header back.

    Registered last in ``create_app`` so it is the outermost middleware: the id
    is set before any inner middleware/handler runs and cleared after.
    """

    @app.middleware("http")
    async def _request_id_middleware(request: Any, call_next: Any) -> Any:
        rid = sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response


class RequestIdFilter(logging.Filter):
    """Inject the active ``request_id`` onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log record, including the request id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "") or None,
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(log_format: str | None = None) -> None:
    """Configure the root logger's format and attach the request-id filter.

    ``log_format`` (or ``NOVAFABRIC_SERVER_LOG_FORMAT``): ``json`` for structured
    logs, anything else for the plain text format. Idempotent — reconfigures the
    root handler in place so single-worker and factory (multi-worker) paths agree.
    """
    import os

    fmt = (log_format or os.environ.get(LOG_FORMAT_ENV) or "text").lower()
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    root.setLevel(logging.INFO)

    formatter: logging.Formatter
    if fmt == "json":
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
        )

    id_filter = RequestIdFilter()
    for handler in root.handlers:
        handler.setFormatter(formatter)
        # Avoid stacking duplicate filters on repeated configure calls.
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(id_filter)
