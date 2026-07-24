"""Transparent MCP proxy (ADR-0015 §Secondary capture path).

Two transport modes:

* **stdio** — spawns an upstream MCP server as a subprocess, forwards
  newline-delimited JSON-RPC 2.0 between client and upstream over pipes.
  Shipped in v0.5.x.
* **HTTP** — listens on a TCP port, forwards HTTP POST + SSE between
  client and an upstream HTTP MCP server. Shipped in v0.6.3 (C-3.4).
  SSE responses are streamed through to the client AND aggregated into
  one ``tool-calls.jsonl`` record per ``tools/call`` (per the C-3.4
  design call: aggregated, not per-chunk).

Every ``tools/call`` request that receives a matching response is recorded
as one ``tool-calls.jsonl`` entry in the active capsule, regardless of
which transport was used. The record schema is identical;
``extensions.io.novafabric.transport_kind`` tells consumers which path
fired (``"stdio"`` or ``"http"``).

Design constraints (apply to both modes):

* **Byte fidelity** for stdio; chunk-stream fidelity for HTTP. The proxy
  parses for inspection only; it never re-serializes the bodies it forwards.
* **No buffering of bodies on the forward path** — pumps stream as they
  arrive. (HTTP mode keeps a separate buffer for record building, bounded
  by the response size.)
* **Stdlib + httpx only.** httpx is already a runtime dep; no new deps.

Still out of scope: ``resources/read``, ``prompts/get``,
``sampling/createMessage``, ``elicitation/create``, multi-upstream
routing, auth, message rewriting.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import IO, TYPE_CHECKING, Any

from novafabric.capture._ulid import new_ulid
from novafabric.mcp.exchanges import ExchangeTracker


def _new_tracker() -> ExchangeTracker:
    return ExchangeTracker()

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass
class _PendingCall:
    method: str
    name: str
    arguments: dict[str, Any]
    request_envelope: dict[str, Any]
    started_at: str
    t0: float


@dataclass
class _ServerIdentity:
    name: str = "unknown"
    version: str = "unknown"
    protocol_version: str = "unknown"


@dataclass
class _ProxyState:
    pending: dict[str, _PendingCall] = field(default_factory=dict)
    server: _ServerIdentity = field(default_factory=_ServerIdentity)
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: SEP-2322 round-trip grouping (NF-038 R3/R4). Guarded by ``lock`` above
    #: rather than locking internally — a second lock would be a second place
    #: for the locking to be wrong.
    exchanges: "ExchangeTracker" = field(default_factory=lambda: _new_tracker())


class MCPProxy:
    """Transparent MCP stdio proxy.

    Parameters
    ----------
    upstream_cmd:
        Command line to spawn the upstream MCP server. ``stdin``/``stdout`` of
        this subprocess are piped; ``stderr`` is inherited so server logs
        remain visible to the user.
    writer:
        Already-opened :class:`CapsuleWriter` whose directory is the active
        capsule. The proxy only appends to ``tool-calls.jsonl``.
    parent_span_id:
        16-char hex span id used as ``parent_span_id`` in emitted records.
    client_in / client_out:
        Binary streams for the parent agent. In production these are the
        proxy process's own ``sys.stdin.buffer`` and ``sys.stdout.buffer``.
        Tests pass ``BytesIO``-like objects.
    """

    def __init__(
        self,
        *,
        upstream_cmd: list[str],
        writer: "CapsuleWriter",
        parent_span_id: str,
        client_in: IO[bytes],
        client_out: IO[bytes],
    ) -> None:
        if not upstream_cmd:
            raise ValueError("upstream_cmd must not be empty")
        self._upstream_cmd = upstream_cmd
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._client_in = client_in
        self._client_out = client_out
        self._state = _ProxyState()

    def run(self) -> int:
        """Spawn upstream, run pumps until either side closes, return exit code."""
        proc = subprocess.Popen(  # noqa: S603 — caller controls upstream_cmd
            self._upstream_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            bufsize=0,
        )
        assert proc.stdin is not None and proc.stdout is not None

        c2s = threading.Thread(
            target=self._pump_client_to_server,
            args=(proc.stdin,),
            name="nova-mcp-proxy-c2s",
            daemon=True,
        )
        s2c = threading.Thread(
            target=self._pump_server_to_client,
            args=(proc.stdout,),
            name="nova-mcp-proxy-s2c",
            daemon=True,
        )
        c2s.start()
        s2c.start()

        try:
            return proc.wait()
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            c2s.join(timeout=1.0)
            s2c.join(timeout=1.0)

    # ------------------------------------------------------------------ pumps

    def _pump_client_to_server(self, server_stdin: IO[bytes]) -> None:
        try:
            for raw in self._client_in:
                try:
                    server_stdin.write(raw)
                    server_stdin.flush()
                except (BrokenPipeError, ValueError):
                    return
                self._inspect_client_message(raw)
        finally:
            try:
                server_stdin.close()
            except Exception:
                pass

    def _pump_server_to_client(self, server_stdout: IO[bytes]) -> None:
        for raw in server_stdout:
            try:
                self._client_out.write(raw)
                self._client_out.flush()
            except (BrokenPipeError, ValueError):
                return
            self._inspect_server_message(raw)

    # ------------------------------------------------------------ inspection

    def _inspect_client_message(self, raw: bytes) -> None:
        msg = _safe_parse(raw)
        if not isinstance(msg, dict):
            return
        # SEP-2322 (NF-038 R3/R4): capture the client's inputResponses leg
        # before the tools/call filter below, which would otherwise drop it.
        self._capture_elicitation(msg, direction="client_to_server")
        method = msg.get("method")
        rpc_id = msg.get("id")
        if rpc_id is None or method != "tools/call":
            return
        params = msg.get("params") or {}
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else {}
        if not isinstance(name, str):
            return
        if not isinstance(arguments, dict):
            arguments = {}
        with self._state.lock:
            self._state.pending[str(rpc_id)] = _PendingCall(
                method=method,
                name=name,
                arguments=arguments,
                request_envelope=msg,
                started_at=_now(),
                t0=time.monotonic(),
            )

    def _inspect_server_message(self, raw: bytes) -> None:
        msg = _safe_parse(raw)
        if not isinstance(msg, dict):
            return
        # SEP-2322 (NF-038 R3/R4): the server-initiated inputRequired leg is
        # a request, so it never matches the pending-response path below.
        self._capture_elicitation(msg, direction="server_to_client")
        rpc_id = msg.get("id")
        if rpc_id is None:
            return
        if "result" in msg and isinstance(msg.get("result"), dict):
            self._maybe_capture_initialize(msg["result"])
        with self._state.lock:
            pending = self._state.pending.pop(str(rpc_id), None)
        if pending is None:
            return
        finished_at = _now()
        duration_ms = int((time.monotonic() - pending.t0) * 1000)
        record = self._build_record(
            pending=pending,
            response_envelope=msg,
            finished_at=finished_at,
            duration_ms=duration_ms,
        )
        self._writer.append_tool_call(record)

    def _capture_elicitation(self, msg: dict[str, Any], *, direction: str) -> None:
        """Record one SEP-2322 leg, if this message is one (NF-038 R3–R5).

        Fail-open like the rest of the proxy: a capture fault must never
        interrupt the proxied exchange, because the user's MCP session
        matters more than our evidence of it.
        """
        try:
            with self._state.lock:
                record = self._state.exchanges.observe(
                    msg,
                    direction=direction,
                    protocol_version=self._state.server.protocol_version,
                )
            if record is not None:
                self._writer.append_tool_call(record)
        except Exception:  # noqa: BLE001 — capture must not break the proxy
            pass

    def _maybe_capture_initialize(self, result: dict[str, Any]) -> None:
        server_info = result.get("serverInfo")
        if isinstance(server_info, dict):
            name = server_info.get("name")
            version = server_info.get("version")
            with self._state.lock:
                if isinstance(name, str) and name:
                    self._state.server.name = name
                if isinstance(version, str) and version:
                    self._state.server.version = version
        proto = result.get("protocolVersion")
        if isinstance(proto, str) and proto:
            with self._state.lock:
                self._state.server.protocol_version = proto
            # R10: note an unfamiliar version but keep forwarding — refusing
            # would break a working client to protect a secondary guarantee.
            from novafabric.mcp.exchanges import protocol_version_note

            note = protocol_version_note(proto)
            if note:
                import logging

                logging.getLogger(__name__).warning("%s", note)

    # ------------------------------------------------------------ recording

    def _build_record(
        self,
        *,
        pending: _PendingCall,
        response_envelope: dict[str, Any],
        finished_at: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        with self._state.lock:
            server = _ServerIdentity(
                name=self._state.server.name,
                version=self._state.server.version,
                protocol_version=self._state.server.protocol_version,
            )
        is_error = "error" in response_envelope
        record: dict[str, Any] = {
            "schema_version": "0.1.0",
            "tool_call_id": new_ulid(),
            "parent_span_id": self._parent_span_id,
            "started_at": pending.started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "tool_name": pending.name,
            "tool_version": "unknown",
            "tool_provider": f"mcp://{server.name}",
            "transport": "mcp",
            "mutates": False,
            "mutation_class": "unknown",
            "arguments": pending.arguments,
            "arguments_schema_ref": None,
            "result": None,
            "result_schema_ref": None,
            "status": "error" if is_error else "success",
            "agent_call_id": None,
            "mcp": {
                "server_name": server.name,
                "server_version": server.version,
                "method": pending.method,
                "request_id": str(pending.request_envelope.get("id", "")),
                "envelope": pending.request_envelope,
                "response_envelope": response_envelope,
            },
            "extensions": {
                "io.novafabric.capture_method": "proxy",
                "io.novafabric.mcp_protocol_version": server.protocol_version,
            },
        }
        if is_error:
            err = response_envelope.get("error") or {}
            record["error"] = {
                "type": "JsonRpcError",
                "message": str(err.get("message", "")),
                "traceback_ref": None,
            }
        else:
            result = response_envelope.get("result")
            if isinstance(result, dict):
                record["result"] = result
        return record


def _safe_parse(raw: bytes) -> Any:
    """Parse a raw line as JSON; return None on any failure."""
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ── HTTP/SSE transport (C-3.4) ───────────────────────────────────────────────
#
# Listens on a TCP port; forwards HTTP POST + SSE between client and an
# upstream MCP HTTP server. SSE responses are aggregated into one
# tool-calls.jsonl record per tools/call (per the design committed in
# v0.6.2 release notes).


def _record_from_http_call(
    *,
    request_envelope: dict[str, Any],
    response_envelope: dict[str, Any],
    started_at: str,
    finished_at: str,
    duration_ms: int,
    parent_span_id: str,
    server: "_ServerIdentity",
    streamed: bool,
    chunk_count: int,
) -> dict[str, Any]:
    """Build a tool-call record from an HTTP-mode tools/call exchange.

    Schema-identical to stdio-mode records except for the
    ``extensions.io.novafabric.transport_kind`` and the optional
    ``extensions.io.novafabric.streaming`` block.
    """
    method = request_envelope.get("method", "tools/call")
    params = request_envelope.get("params") or {}
    name = ""
    arguments: dict[str, Any] = {}
    if isinstance(params, dict):
        if isinstance(params.get("name"), str):
            name = params["name"]
        if isinstance(params.get("arguments"), dict):
            arguments = params["arguments"]
    is_error = "error" in response_envelope
    record: dict[str, Any] = {
        "schema_version": "0.1.0",
        "tool_call_id": new_ulid(),
        "parent_span_id": parent_span_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "tool_name": name,
        "tool_version": "unknown",
        "tool_provider": f"mcp://{server.name}",
        "transport": "mcp",
        "mutates": False,
        "mutation_class": "unknown",
        "arguments": arguments,
        "arguments_schema_ref": None,
        "result": None,
        "result_schema_ref": None,
        "status": "error" if is_error else "success",
        "agent_call_id": None,
        "mcp": {
            "server_name": server.name,
            "server_version": server.version,
            "method": method,
            "request_id": str(request_envelope.get("id", "")),
            "envelope": request_envelope,
            "response_envelope": response_envelope,
        },
        "extensions": {
            "io.novafabric.capture_method": "proxy",
            "io.novafabric.mcp_protocol_version": server.protocol_version,
            "io.novafabric.transport_kind": "http",
        },
    }
    if streamed:
        record["extensions"]["io.novafabric.streaming"] = {
            "streamed": True,
            "chunk_count": chunk_count,
        }
    if is_error:
        err = response_envelope.get("error") or {}
        record["error"] = {
            "type": "JsonRpcError",
            "message": str(err.get("message", "")),
            "traceback_ref": None,
        }
    else:
        result = response_envelope.get("result")
        if isinstance(result, dict):
            record["result"] = result
    return record


def _aggregate_sse_for_response(body: bytes, request_id: Any) -> dict[str, Any]:
    """Walk an SSE byte stream, parse JSON-RPC payloads in ``data:`` lines,
    and return the one whose ``id`` matches the request's ``id``.

    SSE format: events separated by blank lines; each event has
    ``data:``-prefixed lines. The MCP HTTP+SSE transport sends each
    JSON-RPC message as one SSE event; progress notifications carry no
    ``id``, only the final response carries the request's ``id``.

    If no matching response is found, returns an empty dict — caller
    can treat that as "no record" or build an error envelope.
    """
    target = str(request_id)
    chunks: list[bytes] = []
    for line in body.split(b"\n"):
        if line.startswith(b"data:"):
            chunks.append(line[5:].strip())
        elif not line.strip() and chunks:
            payload = b"".join(chunks)
            chunks = []
            try:
                msg = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(msg, dict) and str(msg.get("id", "")) == target:
                return msg
    # Trailing event without blank-line terminator.
    if chunks:
        try:
            msg = json.loads(b"".join(chunks))
        except (json.JSONDecodeError, UnicodeDecodeError):
            msg = None
        if isinstance(msg, dict) and str(msg.get("id", "")) == target:
            return msg
    return {}


def _count_sse_events(body: bytes) -> int:
    """Count the number of SSE events in a body (one event per
    blank-line-terminated block containing at least one ``data:`` line)."""
    count = 0
    has_data = False
    for line in body.split(b"\n"):
        if line.startswith(b"data:"):
            has_data = True
        elif not line.strip():
            if has_data:
                count += 1
            has_data = False
    if has_data:
        count += 1
    return count


class MCPHttpProxy:
    """Transparent MCP HTTP proxy.

    Listens on a TCP port; forwards POST requests to an upstream HTTP MCP
    server and streams responses (JSON or SSE) back to the client. Every
    ``tools/call`` request that receives a matching response is recorded
    as one ``tool-calls.jsonl`` entry, with SSE-aggregated content
    flagged via ``extensions.io.novafabric.streaming``.
    """

    def __init__(
        self,
        *,
        listen_host: str,
        listen_port: int,
        upstream_url: str,
        writer: "CapsuleWriter",
        parent_span_id: str,
        upstream_timeout_s: float = 60.0,
    ) -> None:
        if not upstream_url.startswith(("http://", "https://")):
            raise ValueError(
                f"upstream_url must start with http:// or https://, got {upstream_url!r}"
            )
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._upstream_url = upstream_url.rstrip("/")
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._upstream_timeout_s = upstream_timeout_s
        self._state = _ProxyState()
        self._server: Any = None  # set in run()

    def serve_one_request_for_test(self, request_body: bytes) -> tuple[int, bytes, str | None]:
        """Test seam: process a single client POST as if received over HTTP,
        without binding a real socket. Returns (status_code, body, content_type).

        The actual HTTP server uses the same internal logic
        (:meth:`_handle_request_bytes`) so this function and the
        BaseHTTPRequestHandler path behave identically.
        """
        return self._handle_request_bytes(request_body)

    def _handle_request_bytes(
        self, request_body: bytes
    ) -> tuple[int, bytes, str | None]:
        """Forward ``request_body`` upstream, return (status, body, content-type).

        Records the tools/call into ``tool-calls.jsonl`` if the request's
        method is ``tools/call`` and a matching response is received.
        """
        import httpx

        request_msg = _safe_parse(request_body)
        is_tools_call = (
            isinstance(request_msg, dict)
            and request_msg.get("method") == "tools/call"
            and request_msg.get("id") is not None
        )

        started_at = _now()
        t0 = time.monotonic()
        try:
            response = httpx.post(
                self._upstream_url,
                content=request_body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                timeout=self._upstream_timeout_s,
            )
        except httpx.HTTPError as exc:
            # Upstream unreachable. Synthesize a JSON-RPC error response so
            # the client gets something structured back.
            error_body = json.dumps({
                "jsonrpc": "2.0",
                "id": (request_msg or {}).get("id") if isinstance(request_msg, dict) else None,
                "error": {
                    "code": -32000,
                    "message": f"upstream unreachable: {exc}",
                },
            }).encode()
            if is_tools_call and isinstance(request_msg, dict):
                self._record_tools_call(
                    request_msg=request_msg,
                    response_msg=json.loads(error_body),
                    started_at=started_at,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    streamed=False,
                    chunk_count=0,
                )
            return 502, error_body, "application/json"

        duration_ms = int((time.monotonic() - t0) * 1000)
        ct_header = response.headers.get("content-type", "application/json")
        content_type = ct_header.split(";")[0].strip()
        body_bytes = response.content

        # Capture the initialize response's serverInfo so subsequent
        # tools/call records carry the right server identity.
        if isinstance(request_msg, dict) and request_msg.get("method") == "initialize":
            try:
                resp = json.loads(body_bytes) if content_type == "application/json" else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                resp = {}
            if isinstance(resp, dict) and isinstance(resp.get("result"), dict):
                self._capture_initialize(resp["result"])

        if not is_tools_call or not isinstance(request_msg, dict):
            return response.status_code, body_bytes, content_type

        # Build the response envelope to record.
        if content_type == "text/event-stream":
            response_msg = _aggregate_sse_for_response(
                body_bytes, request_msg.get("id")
            )
            chunk_count = _count_sse_events(body_bytes)
            streamed = True
        else:
            try:
                parsed = json.loads(body_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = {}
            response_msg = parsed if isinstance(parsed, dict) else {}
            chunk_count = 0
            streamed = False

        self._record_tools_call(
            request_msg=request_msg,
            response_msg=response_msg,
            started_at=started_at,
            duration_ms=duration_ms,
            streamed=streamed,
            chunk_count=chunk_count,
        )
        return response.status_code, body_bytes, content_type

    def _record_tools_call(
        self,
        *,
        request_msg: dict[str, Any],
        response_msg: dict[str, Any],
        started_at: str,
        duration_ms: int,
        streamed: bool,
        chunk_count: int,
    ) -> None:
        with self._state.lock:
            server = _ServerIdentity(
                name=self._state.server.name,
                version=self._state.server.version,
                protocol_version=self._state.server.protocol_version,
            )
        record = _record_from_http_call(
            request_envelope=request_msg,
            response_envelope=response_msg,
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            parent_span_id=self._parent_span_id,
            server=server,
            streamed=streamed,
            chunk_count=chunk_count,
        )
        try:
            self._writer.append_tool_call(record)
        except Exception:
            # Bookkeeping failure must never break the proxy.
            pass

    def _capture_initialize(self, result: dict[str, Any]) -> None:
        server_info = result.get("serverInfo")
        with self._state.lock:
            if isinstance(server_info, dict):
                name = server_info.get("name")
                version = server_info.get("version")
                if isinstance(name, str) and name:
                    self._state.server.name = name
                if isinstance(version, str) and version:
                    self._state.server.version = version
            proto = result.get("protocolVersion")
            if isinstance(proto, str) and proto:
                self._state.server.protocol_version = proto

    def run(self) -> int:
        """Start the HTTP server and serve until shutdown signal.

        Returns the exit code (0 on clean shutdown). Blocks the calling
        thread.
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        proxy_self = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — stdlib API
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                status, response_body, content_type = proxy_self._handle_request_bytes(body)
                self.send_response(status)
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if response_body:
                    self.wfile.write(response_body)

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
                # Silence stdlib's per-request stderr logging; the proxy is
                # a transport, not a web server.
                return

        with ThreadingHTTPServer((self._listen_host, self._listen_port), _Handler) as srv:
            self._server = srv
            srv.serve_forever()
        return 0
