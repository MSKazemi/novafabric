"""Strict, loopback-only fixture model server for EvidenceLab D1.

It serves one OpenAI-compatible endpoint, ``POST /v1/chat/completions``, and
answers exactly one request: the one the scenario's own fixtures predict. Every
other shape is refused with a named ``E_*`` error and *no* ``choices`` — there
is no generic "success" fallback, and the response is never chosen from a mode
header (the BlackBox demo's ``X-Demo-Mode`` is deliberately ignored here).

The server binds ``127.0.0.1`` only (there is no host option), takes its port
from the OS unless told otherwise, and stops on SIGTERM or after a maximum
lifetime so an orphaned server cannot linger.

Usage (the harness does this for you)::

    python mock_llm_server.py --scenario-dir <dir> --ready-file ready.json \\
        --request-log requests.jsonl
"""

from __future__ import annotations

import argparse
import errno
import http.server
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import evidence_scenario as lab

HOST = "127.0.0.1"
CHAT_PATH = "/v1/chat/completions"
_FIXED_CREATED = 1767225600  # 2026-01-01T00:00:00Z — a constant, so replies are reproducible
_MAX_LOG_ENTRIES = 1000
_ALLOWED_FIELDS = {"model", "seed", "temperature", "messages"}


class _Reject(Exception):
    def __init__(self, status: int, code: str, message: str, received: int | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.received = received  # body bytes read when the refusal knows it, else None


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "evidencelab_request_rejected", "code": code}}


class EvidenceLabServer(http.server.HTTPServer):
    """One scenario, one expected request, consumed at most once."""

    allow_reuse_address = True

    def __init__(
        self,
        scenario: lab.Scenario,
        port: int = 0,
        log_path: Path | None = None,
        request_timeout: float = 10.0,
    ) -> None:
        super().__init__((HOST, port), _Handler)
        self.scenario = scenario
        self.request_timeout = request_timeout
        self.log_path = log_path
        self.entries: list[dict[str, Any]] = []
        self.consumed = False
        self.expected_body = lab.build_request_body(scenario, lab.expected_observations(scenario))

    def record(self, entry: dict[str, Any]) -> None:
        if len(self.entries) >= _MAX_LOG_ENTRIES:
            return
        entry = {"seq": len(self.entries) + 1, **entry}
        self.entries.append(entry)
        if self.log_path is not None:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")


def make_server(
    scenario: lab.Scenario,
    *,
    port: int = 0,
    log_path: Path | None = None,
    request_timeout: float = 10.0,
) -> EvidenceLabServer:
    return EvidenceLabServer(
        scenario, port=port, log_path=log_path, request_timeout=request_timeout
    )


class _Handler(http.server.BaseHTTPRequestHandler):
    server: EvidenceLabServer
    protocol_version = "HTTP/1.0"

    def setup(self) -> None:
        # A per-recv timeout ends a stalled client, but a client sending one byte at a time
        # resets it forever. The timer is an overall cap on the whole connection: it stops
        # the read side only, so the refusal can still be written back.
        self.timeout = self.server.request_timeout  # type: ignore[misc]  # per connection
        super().setup()
        self._deadline = threading.Timer(2 * self.server.request_timeout, self._expire)
        self._deadline.daemon = True
        self._deadline.start()

    def _expire(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass  # the connection is already closed

    def finish(self) -> None:
        self._deadline.cancel()
        super().finish()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # the structured request log replaces the default stderr access log

    # Every method goes through one path so a GET is refused, not a 501.
    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_GET  # noqa: N815

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def _serve(self) -> None:
        sha: str | None = None
        size = 0
        outcome, code, status = "rejected", None, 0
        payload: dict[str, Any] = {}
        try:
            body, size = self._read_checked_body()
            parsed = self._parse(body)
            sha = lab.sha256_hex(lab.canonical_json(parsed))
            self._validate(parsed)
            if self.server.consumed:
                raise _Reject(409, "E_REQUEST_REPEATED", "the expected request was already served")
            self.server.consumed = True
            payload = self._completion(parsed)
            outcome, status = "accepted", 200
        except _Reject as rejection:
            code, status = rejection.code, rejection.status
            if rejection.received is not None:
                size = rejection.received
            payload = _error_payload(rejection.code, rejection.message)
        except TimeoutError:
            outcome, code, status = "error", "E_REQUEST_TIMEOUT", 408
            payload = _error_payload(code, "the request was not completed in time")
        except Exception:  # noqa: BLE001 - a request must never vanish without a log line
            outcome, code, status = "error", "E_INTERNAL", 500
            payload = _error_payload(code, "the mock failed on this request")
        self.server.record(
            {
                "method": self.command,
                "path": self.path,
                "outcome": outcome,
                "code": code,
                "status": status,
                "request_sha256": sha,
                "body_bytes": size,
            }
        )
        try:
            self._send(status, payload)
        except OSError:
            pass  # the client already went away; the log line above is the record

    # ── request checks ───────────────────────────────────────────────────────

    def _read_checked_body(self) -> tuple[bytes, int]:
        if self.command != "POST":
            raise _Reject(405, "E_METHOD_NOT_ALLOWED", "only POST is served")
        if self.path != CHAT_PATH:
            raise _Reject(404, "E_PATH_NOT_FOUND", f"only {CHAT_PATH} is served")
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise _Reject(415, "E_CONTENT_TYPE", "Content-Type must be application/json")
        if self.headers.get("Transfer-Encoding") is not None:
            raise _Reject(411, "E_LENGTH_REQUIRED", "chunked bodies are not accepted")
        declared = self.headers.get("Content-Length")
        if declared is None or not (declared.isascii() and declared.isdigit()):
            raise _Reject(411, "E_LENGTH_REQUIRED", "a numeric Content-Length is required")
        length = int(declared)
        limit = self.server.scenario.manifest.limits.max_request_bytes
        if length > limit:
            self._drain(min(length, 2 * limit))
            raise _Reject(413, "E_REQUEST_TOO_LARGE", f"body is larger than {limit} bytes")
        body = self.rfile.read(length)  # returns early, with fewer bytes, if the client hangs up
        if len(body) != length:
            raise _Reject(
                400,
                "E_BODY_TRUNCATED",
                f"Content-Length declared {length} bytes but {len(body)} arrived",
                received=len(body),
            )
        return body, length

    def _drain(self, count: int) -> None:
        # Read (and discard) what the client already sent so closing the socket does
        # not reset the connection before the client has read our 413.
        remaining = count
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    @staticmethod
    def _parse(body: bytes) -> dict[str, Any]:
        try:
            parsed = json.loads(body)
        except (ValueError, RecursionError) as exc:  # deep nesting raises RecursionError
            raise _Reject(400, "E_INVALID_JSON", "body is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise _Reject(400, "E_BODY_NOT_OBJECT", "body must be a JSON object")
        return parsed

    def _validate(self, body: dict[str, Any]) -> None:
        scn = self.server.scenario
        extra = sorted(set(body) - _ALLOWED_FIELDS)
        if extra:
            raise _Reject(400, "E_UNEXPECTED_FIELD", f"unexpected request fields: {extra}")
        missing = sorted(_ALLOWED_FIELDS - set(body))
        if missing:
            raise _Reject(400, "E_MISSING_FIELD", f"missing request fields: {missing}")
        if body["model"] != scn.manifest.model_id:
            raise _Reject(400, "E_MODEL_MISMATCH", f"model must be {scn.manifest.model_id!r}")
        messages = body["messages"]
        well_formed = (
            isinstance(messages, list)
            and all(isinstance(m, dict) and set(m) == {"role", "content"} for m in messages)
            and all(isinstance(m["content"], str) for m in messages)
        )
        if not well_formed or [m["role"] for m in messages] != ["system", "user"]:
            raise _Reject(400, "E_ROLE_SEQUENCE", "messages must be exactly [system, user]")
        seed_ok = type(body["seed"]) is int and body["seed"] == scn.manifest.seed  # noqa: E721
        temp_ok = type(body["temperature"]) in (int, float) and body["temperature"] == 0
        if not (seed_ok and temp_ok):
            raise _Reject(400, "E_PARAMETER_MISMATCH", "seed/temperature differ from the scenario")
        if messages[0]["content"] != scn.prompt:
            raise _Reject(400, "E_PROMPT_MISMATCH", "the system prompt is not this scenario's")
        self._validate_user_content(messages[1]["content"])
        if lab.canonical_json(body) != lab.canonical_json(self.server.expected_body):
            raise _Reject(400, "E_REQUEST_MISMATCH", "the request is not the expected transcript")

    def _validate_user_content(self, content: str) -> None:
        scn = self.server.scenario
        try:
            user = json.loads(content)
        except ValueError as exc:
            raise _Reject(400, "E_USER_CONTENT_INVALID", "user content is not JSON") from exc
        if not isinstance(user, dict) or set(user) != {
            "scenario_id",
            "prompt_revision",
            "observations",
        }:
            raise _Reject(400, "E_USER_CONTENT_INVALID", "user content has the wrong keys")
        if user["scenario_id"] != scn.manifest.scenario_id:
            raise _Reject(400, "E_SCENARIO_MISMATCH", "the request names another scenario")
        if user["prompt_revision"] != scn.prompt_revision:
            raise _Reject(400, "E_PROMPT_REVISION_MISMATCH", "the prompt revision differs")
        observed = user["observations"]
        expected = lab.expected_observations(scn)
        if not isinstance(observed, dict):
            raise _Reject(400, "E_OBSERVATION_MISSING", "observations must be an object")
        for name in expected:
            if name not in observed:
                raise _Reject(400, "E_OBSERVATION_MISSING", f"observation {name!r} is missing")
        if set(observed) != set(expected):
            raise _Reject(400, "E_OBSERVATION_MISMATCH", "unexpected extra observations")
        for name, want in expected.items():
            if lab.sha256_hex(lab.canonical_json(observed[name])) != lab.sha256_hex(
                lab.canonical_json(want)
            ):
                raise _Reject(
                    400, "E_OBSERVATION_MISMATCH", f"observation {name!r} differs from the fixture"
                )

    # ── reply ────────────────────────────────────────────────────────────────

    def _completion(self, body: dict[str, Any]) -> dict[str, Any]:
        scn = self.server.scenario
        digest = lab.sha256_hex(lab.canonical_json(body))
        content = lab.canonical_json(scn.model_response)
        prompt_chars = sum(len(m["content"]) for m in body["messages"])
        prompt_tokens = prompt_chars // 4
        completion_tokens = len(content) // 4
        return {
            "id": f"chatcmpl-evidencelab-{digest[:12]}",
            "object": "chat.completion",
            "created": _FIXED_CREATED,
            "model": scn.manifest.model_id,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-NovaFabric-Demo", "synthetic")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)


# ── command line ─────────────────────────────────────────────────────────────


def _fail(code: str, message: str) -> int:
    sys.stderr.write(f"EVIDENCELAB_ERROR code={code} message={message}\n")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EvidenceLab strict loopback fixture model")
    parser.add_argument("--scenario-dir", required=True, type=Path)
    parser.add_argument(
        "--variant", choices=lab.VARIANTS, default=lab.BASE_VARIANT,
        help="which controlled input variant's single request this server accepts",
    )  # fmt: skip
    parser.add_argument("--port", type=int, default=0, help="0 = let the OS pick an ephemeral port")
    parser.add_argument("--ready-file", type=Path, help="written once the server is listening")
    parser.add_argument("--request-log", type=Path, help="JSONL log of every request outcome")
    parser.add_argument("--max-lifetime", type=float, help="seconds before the server stops itself")
    args = parser.parse_args(argv)

    try:
        scenario = lab.load_scenario(args.scenario_dir, args.variant)
    except lab.EvidenceLabError as exc:
        return _fail(exc.code, exc.message)
    try:
        server = make_server(scenario, port=args.port, log_path=args.request_log)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return _fail("E_MOCK_PORT_IN_USE", f"port {args.port} on {HOST} is already in use")
        return _fail("E_MOCK_START_FAILED", f"cannot bind {HOST}:{args.port}: {exc.strerror}")

    def stop(_signum: int, _frame: object) -> None:
        # shutdown() blocks until serve_forever() returns, so it must not run on
        # the thread that is inside serve_forever().
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    lifetime = args.max_lifetime or float(scenario.manifest.limits.timeout_seconds + 60)
    timer = threading.Timer(lifetime, server.shutdown)
    timer.daemon = True
    timer.start()

    if args.ready_file is not None:
        info = {"host": HOST, "port": server.server_address[1], "pid": os.getpid()}
        partial = args.ready_file.with_name(args.ready_file.name + ".partial")
        partial.write_text(json.dumps(info))
        partial.replace(args.ready_file)  # atomic: a reader never sees half a file
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        timer.cancel()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
