"""Mock OpenAI-compatible chat completions server for the blackbox_demo.

Listens on http://127.0.0.1:9099 and serves canned responses for
POST /v1/chat/completions.  No real API key, no network calls.

Mode selection: the client sets X-Demo-Mode: bad|fixed in the request
header.  agent.py passes this via openai.OpenAI(default_headers=...).

Graceful shutdown: responds to SIGTERM and SIGINT.
"""
from __future__ import annotations

import json
import signal
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

HOST = "127.0.0.1"
PORT = 9099

BAD_CONTENT = json.dumps({
    "action": "disable_rate_limiting",
    "reason": "Disabling rate limiting increases throughput for high-volume transactions.",
    "value": False,
})

FIXED_CONTENT = json.dumps({
    "action": "reduce_max_connections",
    "reason": "Reducing max_connections from 1000 to 500 prevents connection exhaustion under burst load.",
    "value": 500,
})


def _make_completion(content: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-demo-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "gpt-4o-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 80,
            "completion_tokens": 30,
            "total_tokens": 110,
        },
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"mock_llm: {fmt % args}\n")

    def do_POST(self) -> None:  # noqa: N802
        # Accept both /chat/completions (openai SDK default when base_url has no path)
        # and /v1/chat/completions (when base_url is set to http://host/v1).
        if self.path not in ("/chat/completions", "/v1/chat/completions"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)

        mode = self.headers.get("X-Demo-Mode", "bad")
        content = FIXED_CONTENT if mode == "fixed" else BAD_CONTENT
        body = json.dumps(_make_completion(content)).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    HTTPServer.allow_reuse_address = True
    server = HTTPServer((HOST, PORT), _Handler)

    def _shutdown(sig: int, frame: Any) -> None:
        sys.stderr.write(f"\nmock_llm: received signal {sig}, shutting down\n")
        server.server_close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    sys.stderr.write(f"mock_llm: listening on http://{HOST}:{PORT}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
