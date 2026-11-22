"""Strict, deterministic OpenAI-compatible fixture server for blackbox_demo.

Only the two documented synthetic requests are supported.  The server binds to
loopback and never calls a real model.  It is an example, not a general-purpose
OpenAI proxy or a trustworthy evaluator of model behavior.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

HOST = "127.0.0.1"
MODEL = "novafabric-mock-v1"
MAX_BODY_BYTES = 16 * 1024
FIXTURES = Path(__file__).parent / "fixtures"

BAD_CONTENT = json.dumps(
    {
        "action": "disable_rate_limiting",
        "reason": "Disabling rate limiting increases throughput for high-volume transactions.",
        "value": False,
    }
)
FIXED_CONTENT = json.dumps(
    {
        "action": "reduce_max_connections",
        "reason": "Reducing max_connections from 1000 to 500 prevents connection exhaustion "
        "under burst load.",
        "value": 500,
    }
)


@lru_cache(maxsize=1)
def _expected_messages() -> list[dict[str, str]]:
    prompt = (FIXTURES / "prompt.txt").read_text(encoding="utf-8")
    config = (FIXTURES / "service.yaml").read_text(encoding="utf-8")
    return [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Config:\n{config}"},
    ]


def _validate_request(payload: Any, mode: str | None) -> str:
    """Return one of the two fixture modes or raise ValueError (never guess)."""
    if mode is None or mode not in ("bad", "fixed"):
        raise ValueError("X-Demo-Mode must be bad or fixed")
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    if set(payload) - {"model", "messages", "stream"}:
        raise ValueError("unsupported request fields")
    if payload.get("model") != MODEL:
        raise ValueError("unknown synthetic model")
    if payload.get("stream", False) is not False:
        raise ValueError("streaming is not supported by this fixture")
    if payload.get("messages") != _expected_messages():
        raise ValueError("messages differ from the pinned synthetic scenario")
    return mode


def _make_completion(mode: str) -> dict[str, Any]:
    content = BAD_CONTENT if mode == "bad" else FIXED_CONTENT
    return {
        "id": f"chatcmpl-blackbox-{mode}-v1",
        "object": "chat.completion",
        "created": 0,  # Fixture value, deliberately not a real event timestamp.
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110},
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        # Never log prompt contents or request payloads.
        sys.stderr.write(f"mock_llm: {fmt % args}\n")

    def _json_response(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-NovaFabric-Demo", "synthetic")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _reject(self, status: HTTPStatus, code: str, reason: str) -> None:
        self._json_response(
            status, {"error": {"type": "invalid_request_error", "code": code, "message": reason}}
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/chat/completions", "/v1/chat/completions"):
            self._reject(HTTPStatus.NOT_FOUND, "unknown_route", "unknown fixture route")
            return
        if self.headers.get("Transfer-Encoding"):
            self._reject(
                HTTPStatus.BAD_REQUEST, "unsupported_encoding", "chunked requests are unsupported"
            )
            return
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdecimal() or len(raw_length) > 8 or int(raw_length) <= 0:
            self._reject(
                HTTPStatus.BAD_REQUEST, "invalid_length", "positive Content-Length required"
            )
            return
        if int(raw_length) > MAX_BODY_BYTES:
            self._reject(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                "fixture request exceeds size limit",
            )
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._reject(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "invalid_media_type",
                "application/json required",
            )
            return
        try:
            payload = json.loads(self.rfile.read(int(raw_length)))
            mode = _validate_request(payload, self.headers.get("X-Demo-Mode"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(HTTPStatus.BAD_REQUEST, "invalid_json", "request is not valid UTF-8 JSON")
            return
        except ValueError as exc:
            self._reject(HTTPStatus.BAD_REQUEST, "scenario_mismatch", str(exc))
            return
        self._json_response(HTTPStatus.OK, _make_completion(mode))


def main() -> None:
    # Validate port at entry time, not import time (importable by fixture tests).
    port = int(os.environ.get("DEMO_PORT", "9099"))
    server = HTTPServer((HOST, port), _Handler)

    def _shutdown(sig: int, frame: Any) -> None:
        sys.stderr.write(f"mock_llm: received signal {sig}, shutting down\n")
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    sys.stderr.write(f"mock_llm: SYNTHETIC fixture listening on http://{HOST}:{port}\n")
    server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
