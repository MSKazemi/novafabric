"""Fixtures for the ADR-0137 lifecycle-event tests."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

_EVENT_ENV_VARS = [
    "NOVA_EVENTS_LOG",
    "NOVA_EVENTS_WEBHOOK",
    "NOVA_EVENTS_MAX_RETRIES",
    "NOVA_EVENTS_TIMEOUT_S",
    "NOVA_EVENTS_SIGN_SECRET",
    "NOVA_EVENTS_SIGN_KEYID",
    # ADR-0192 operational alerts — same opt-in discipline.
    "NOVA_ALERTS_WEBHOOK",
    "NOVA_ALERTS_MIN_SEVERITY",
    "NOVA_ALERTS_TYPES",
    "NOVA_ALERTS_DEDUP_WINDOW_S",
    "NOVA_ALERTS_DEDUP_MAX_ENTRIES",
    "NOVA_ALERTS_MAX_RETRIES",
    "NOVA_ALERTS_TIMEOUT_S",
    "NOVA_ALERTS_SIGN_SECRET",
    "NOVA_ALERTS_SIGN_KEYID",
    "NOVA_ALERTS_AUDIT_LOG",
]


@pytest.fixture(autouse=True)
def _clean_events_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts unconfigured — the opt-in default."""
    for var in _EVENT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@dataclass
class RecordingWebhook:
    url: str
    server: ThreadingHTTPServer
    received: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def webhook_server() -> Iterator[RecordingWebhook]:
    """Local test HTTP server recording every POST it receives."""
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — http.server API
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            received.append({"body": body, "headers": dict(self.headers)})
            code = getattr(self.server, "response_code", 200)
            self.send_response(code)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hook = RecordingWebhook(
        url=f"http://127.0.0.1:{server.server_port}/nova-hook",
        server=server,
        received=received,
    )
    yield hook
    server.shutdown()
    server.server_close()
