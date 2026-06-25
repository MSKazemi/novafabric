"""LLM API proxy (ADR-0026 / Path A / v0.6.4).

A transparent HTTP proxy for non-Python LLM clients (Claude Code, Cursor,
Continue, custom Node/Go/Rust agents, …). Listens on a TCP port, forwards
POST requests to an upstream LLM API URL, and records each call into
the active capsule's ``model-calls.jsonl``.

Scope locked by ADR-0026:

* **Base-URL override only.** Users configure their client to point at
  the proxy (e.g. ``OPENAI_BASE_URL=http://127.0.0.1:8765``). Permanently
  rejected: TLS MITM with a local CA.
* **Captures into model-calls.jsonl** (LLM calls), reusing the wire-level
  pipeline: URL registry → body adapter → OTel GenAI extractor.
* **Stdlib + httpx only.**
* **Non-streaming responses captured fully.** Streaming SSE responses
  (OpenAI-style delta streams) are forwarded through unchanged with a
  ``streaming: true`` flag in the record; full delta merge is queued for
  v0.6.5.

Anti-patterns (per ADR-0026):

* No request modification — proxy is record-only. Never rewrite prompts
  or mutate payloads.
* No phone-home / heartbeat.
* No multi-upstream routing in a single proxy instance.
* No caching of LLM responses.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from novafabric.capture._ulid import new_ulid
from novafabric.capture.hooks._otel_genai import (
    build_record_envelope,
    extract_request_attributes,
)
from novafabric.capture.hooks._url_registry import load_url_registry

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _count_sse_events(body: bytes) -> int:
    """Count blank-line-separated SSE event blocks in a body."""
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


def _iter_sse_data_payloads(body: bytes) -> list[bytes]:
    """Yield each SSE event's ``data:`` payload as bytes.

    The OpenAI Chat Completions streaming format uses one ``data:`` line
    per event. The terminal sentinel is ``data: [DONE]``. We return raw
    payloads (still bytes); callers parse JSON.
    """
    out: list[bytes] = []
    for line in body.split(b"\n"):
        if line.startswith(b"data:"):
            payload = line[5:].strip()
            if payload:
                out.append(payload)
    return out


def _merge_anthropic_streaming_response(body: bytes) -> dict[str, Any]:
    """Walk an Anthropic SSE delta stream, merge events, return a
    synthesized non-streaming response envelope.

    Anthropic's streaming uses a different event-type model than
    OpenAI:

    * ``message_start`` — payload has ``message`` with id, model, usage
    * ``content_block_start`` — declares a content block at index N
      (``text`` or ``tool_use``)
    * ``content_block_delta`` — extends a block; ``text_delta`` for
      text, ``input_json_delta`` for tool input arguments
    * ``content_block_stop`` — closes a block (no payload changes)
    * ``message_delta`` — top-level updates: ``stop_reason``,
      cumulative ``usage.output_tokens``
    * ``message_stop`` — terminal sentinel

    The synthesized response mirrors the non-streaming Anthropic API:
    ``{id, model, content: [{type, text|input}, ...], stop_reason,
    usage: {input_tokens, output_tokens}}``.
    """
    response: dict[str, Any] = {}
    blocks: dict[int, dict[str, Any]] = {}

    for raw in _iter_sse_data_payloads(body):
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        ev_type = event.get("type")

        if ev_type == "message_start":
            message = event.get("message") or {}
            if isinstance(message, dict):
                for key in ("id", "model"):
                    value = message.get(key)
                    if isinstance(value, str) and value:
                        response[key] = value
                usage = message.get("usage")
                if isinstance(usage, dict):
                    if isinstance(usage.get("input_tokens"), int):
                        response.setdefault("usage", {})["input_tokens"] = usage["input_tokens"]
        elif ev_type == "content_block_start":
            idx = event.get("index", 0)
            if not isinstance(idx, int):
                idx = 0
            block = event.get("content_block") or {}
            if not isinstance(block, dict):
                block = {}
            # Initialize the slot with a copy of the declared block.
            blocks[idx] = dict(block)
            # Ensure text blocks have a string slot to accumulate into.
            if blocks[idx].get("type") == "text":
                blocks[idx].setdefault("text", "")
            elif blocks[idx].get("type") == "tool_use":
                # Anthropic tool_use blocks: input is built up from
                # input_json_delta events; we accumulate the JSON string
                # then parse at end.
                blocks[idx].setdefault("_input_json", "")
        elif ev_type == "content_block_delta":
            idx = event.get("index", 0)
            if not isinstance(idx, int):
                idx = 0
            slot = blocks.setdefault(idx, {"type": "text", "text": ""})
            delta = event.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                if isinstance(text, str):
                    slot["text"] = (slot.get("text") or "") + text
            elif delta_type == "input_json_delta":
                partial = delta.get("partial_json")
                if isinstance(partial, str):
                    slot["_input_json"] = (slot.get("_input_json") or "") + partial
        elif ev_type == "message_delta":
            delta = event.get("delta") or {}
            if isinstance(delta, dict):
                stop_reason = delta.get("stop_reason")
                if isinstance(stop_reason, str) and stop_reason:
                    response["stop_reason"] = stop_reason
            usage = event.get("usage")
            if isinstance(usage, dict):
                if isinstance(usage.get("output_tokens"), int):
                    response.setdefault("usage", {})["output_tokens"] = usage["output_tokens"]
        # message_stop has no payload to merge; it's just a sentinel.

    # Finalize tool_use blocks by parsing the accumulated JSON.
    for idx, block in blocks.items():
        if block.get("type") == "tool_use" and "_input_json" in block:
            raw_input = block.pop("_input_json")
            try:
                parsed = json.loads(raw_input) if raw_input else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = {}
            block["input"] = parsed if isinstance(parsed, dict) else {}

    if blocks:
        response["content"] = [blocks[i] for i in sorted(blocks.keys())]
    return response


def _merge_streaming_response(body: bytes, gen_ai_system: str) -> dict[str, Any]:
    """Provider-aware dispatcher for SSE delta merging.

    OpenAI / Anthropic each have distinct event-type models. Falls back
    to the OpenAI shape for unknown providers (since OpenAI-compatible
    is the most common gateway shape).
    """
    if gen_ai_system == "anthropic":
        return _merge_anthropic_streaming_response(body)
    return _merge_openai_streaming_response(body)


def _merge_openai_streaming_response(body: bytes) -> dict[str, Any]:
    """Walk an OpenAI SSE delta stream, merge the deltas, return a
    synthesized non-streaming response envelope.

    Each event payload is a dict like::

        {"id": "chatcmpl-…", "model": "gpt-4o-…", "choices": [
            {"index": 0, "delta": {"content": "hello"}}
        ]}

    The terminal event is the literal string ``[DONE]`` (not JSON).
    Content deltas concatenate. Tool-call deltas accumulate by
    ``tool_calls[i].index`` and concatenate the ``arguments`` JSON
    string. Usage (when emitted in the final stream-options chunk) is
    pulled into the synthesized response.

    On malformed input we return whatever we managed to parse —
    capture is a recorder, not a validator.
    """
    response: dict[str, Any] = {}
    # Per-choice accumulators keyed by index.
    choices: dict[int, dict[str, Any]] = {}

    for raw in _iter_sse_data_payloads(body):
        if raw == b"[DONE]":
            continue
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue

        # Top-level fields appear on every event in the stream;
        # keep the latest non-empty one.
        for key in ("id", "model", "object", "system_fingerprint"):
            value = event.get(key)
            if isinstance(value, str) and value:
                response[key] = value
        if "created" in event and isinstance(event["created"], int):
            response["created"] = event["created"]

        usage = event.get("usage")
        if isinstance(usage, dict) and usage:
            # Final usage event (when stream_options.include_usage = true).
            response["usage"] = usage

        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            idx = choice.get("index", 0)
            if not isinstance(idx, int):
                idx = 0
            slot = choices.setdefault(idx, {
                "index": idx,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": None,
            })
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                delta = {}

            role = delta.get("role")
            if isinstance(role, str) and role:
                slot["message"]["role"] = role

            content_delta = delta.get("content")
            if isinstance(content_delta, str):
                # OpenAI sends content as bare strings. Concatenate.
                existing = slot["message"].get("content") or ""
                slot["message"]["content"] = existing + content_delta

            # Tool call deltas: each delta has tool_calls[i].index and
            # may add to id/function.name/function.arguments piecewise.
            tc_deltas = delta.get("tool_calls")
            if isinstance(tc_deltas, list):
                tc_acc: list[dict[str, Any]] = slot["message"].setdefault(
                    "tool_calls", []
                )
                for tc in tc_deltas:
                    if not isinstance(tc, dict):
                        continue
                    tc_idx = tc.get("index", 0)
                    while len(tc_acc) <= tc_idx:
                        tc_acc.append({
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                    target = tc_acc[tc_idx]
                    if isinstance(tc.get("id"), str):
                        target["id"] = tc["id"]
                    if isinstance(tc.get("type"), str):
                        target["type"] = tc["type"]
                    fn = tc.get("function") or {}
                    if isinstance(fn, dict):
                        if isinstance(fn.get("name"), str):
                            target["function"]["name"] = (
                                target["function"].get("name", "") + fn["name"]
                            )
                        if isinstance(fn.get("arguments"), str):
                            target["function"]["arguments"] = (
                                target["function"].get("arguments", "") + fn["arguments"]
                            )

            finish = choice.get("finish_reason")
            if isinstance(finish, str) and finish:
                slot["finish_reason"] = finish

    if choices:
        response["choices"] = [choices[i] for i in sorted(choices.keys())]
    return response


# Headers we strip when forwarding (hop-by-hop or set by the listener).
# Per RFC 7230 §6.1, these MUST NOT be forwarded by intermediaries.
_HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    # We re-set these from the actual response.
    "content-length", "host",
})


def _filter_forwarded_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: v for k, v in headers.items()
        if k.lower() not in _HOP_BY_HOP_HEADERS
    }


class ApiProxy:
    """Transparent LLM API proxy.

    Forwards every POST request to ``upstream_url + path`` and records
    each call as a ``model-calls.jsonl`` entry. GET and other non-POST
    requests are forwarded but not recorded (they are not LLM calls).

    Parameters
    ----------
    listen_host, listen_port:
        TCP listener address.
    upstream_url:
        Base URL of the upstream LLM API. Trailing slashes are stripped.
        Each forwarded request hits ``upstream_url + path``.
    writer:
        Active :class:`CapsuleWriter` whose directory is the capsule.
    parent_span_id:
        16-char hex span id used as ``parent_span_id`` in emitted records.
    upstream_timeout_s:
        Wall-clock timeout for each upstream call. Default 60s.
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
        self._registry = load_url_registry()
        self._server: Any = None  # set in run()

    # ── Test seam: process one request without binding a socket ──────────────

    def serve_one_request_for_test(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, bytes, str | None]:
        """Process a single client request as if received over HTTP.

        Used by unit tests to exercise the request-handler logic without
        binding a port. The real HTTP server uses the same internal
        method (:meth:`_handle_request`).
        """
        return self._handle_request(method, path, headers, body)

    # ── Core request handler (shared with BaseHTTPRequestHandler) ────────────

    def _handle_request(
        self, method: str, path: str, headers: dict[str, str], body: bytes,
    ) -> tuple[int, bytes, str | None]:
        import httpx

        full_upstream = f"{self._upstream_url}{path if path.startswith('/') else '/' + path}"
        forward_headers = _filter_forwarded_headers(headers)

        # Non-POST: forward but don't record (these are not LLM calls).
        if method.upper() != "POST":
            try:
                resp = httpx.request(
                    method, full_upstream,
                    headers=forward_headers,
                    timeout=self._upstream_timeout_s,
                )
            except httpx.HTTPError as exc:
                return 502, json.dumps({
                    "error": {"message": f"upstream unreachable: {exc}"}
                }).encode(), "application/json"
            ct = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
            return resp.status_code, resp.content, ct

        # POST: forward + record as model-call.
        started_at = _now()
        t0 = time.monotonic()
        try:
            response = httpx.post(
                full_upstream,
                content=body,
                headers=forward_headers,
                timeout=self._upstream_timeout_s,
            )
        except httpx.HTTPError as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            error_body = json.dumps({
                "error": {
                    "type": type(exc).__name__,
                    "message": f"upstream unreachable: {exc}",
                }
            }).encode()
            self._record_call(
                request_body=body,
                request_url=full_upstream,
                response_body=b"",
                response_status=502,
                content_type="application/json",
                started_at=started_at,
                duration_ms=duration_ms,
                upstream_error=str(exc),
            )
            return 502, error_body, "application/json"

        duration_ms = int((time.monotonic() - t0) * 1000)
        ct_header = response.headers.get("content-type", "application/json")
        content_type = ct_header.split(";")[0].strip()

        self._record_call(
            request_body=body,
            request_url=full_upstream,
            response_body=response.content,
            response_status=response.status_code,
            content_type=content_type,
            started_at=started_at,
            duration_ms=duration_ms,
            upstream_error=None,
        )
        return response.status_code, response.content, content_type

    # ── Recording ────────────────────────────────────────────────────────────

    def _record_call(
        self,
        *,
        request_body: bytes,
        request_url: str,
        response_body: bytes,
        response_status: int,
        content_type: str,
        started_at: str,
        duration_ms: int,
        upstream_error: str | None,
    ) -> None:
        """Build and persist one model-calls.jsonl record."""
        try:
            body_dict: dict[str, Any] = json.loads(request_body) if request_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            body_dict = {}
        if not isinstance(body_dict, dict):
            body_dict = {}

        gen_ai_system = self._registry.gen_ai_system(request_url)
        # Status reflects the upstream HTTP status, not just the proxy's.
        status = "error" if upstream_error or response_status >= 400 else "success"

        record = build_record_envelope(
            model_call_id=new_ulid(),
            parent_span_id=self._parent_span_id,
            started_at=started_at,
            finished_at=_now(),
            duration_ms=duration_ms,
            status=status,
        )
        # Reuses the full wire-level extraction: registry + adapter + semconv.
        record.update(extract_request_attributes(
            body_dict, url=request_url, gen_ai_system=gen_ai_system,
        ))
        # api-proxy-specific extensions (parallel to MCP-proxy extensions).
        # The base envelope from extract_request_attributes does not include
        # an extensions block, so we add it here.
        record.setdefault("extensions", {})
        ext: dict[str, Any] = record["extensions"]
        ext["io.novafabric.capture_method"] = "api-proxy"
        ext["io.novafabric.transport_kind"] = "http"

        # Streaming responses (v0.6.5): walk the SSE delta stream,
        # synthesize a merged response envelope (content concatenation,
        # tool_calls accumulation, usage extraction), and treat the result
        # as if it were a non-streaming response for record purposes.
        # The original byte stream was already forwarded to the client
        # untouched — this merge only affects what we record.
        if content_type == "text/event-stream":
            ext["io.novafabric.streaming"] = {
                "streamed": True,
                "chunk_count": _count_sse_events(response_body),
            }
            try:
                merged = _merge_streaming_response(response_body, gen_ai_system)
            except Exception:
                merged = {}
            response_id = merged.get("id")
            if isinstance(response_id, str) and response_id:
                record["gen_ai.response.id"] = response_id
            response_model = merged.get("model")
            if isinstance(response_model, str) and response_model:
                record["gen_ai.response.model"] = response_model
            # OpenAI shape uses 'choices'; Anthropic shape uses 'content'
            # + 'stop_reason'. Normalize both into gen_ai.response.choices.
            choices = merged.get("choices")
            if isinstance(choices, list):
                record["gen_ai.response.choices"] = choices
                finish_reasons = [
                    c.get("finish_reason")
                    for c in choices
                    if isinstance(c, dict) and isinstance(c.get("finish_reason"), str)
                ]
                if finish_reasons:
                    record["gen_ai.response.finish_reasons"] = finish_reasons
            elif "content" in merged:
                # Anthropic-shape merged response: synthesize an
                # OpenAI-style choices array so consumers see a stable
                # shape regardless of which provider was captured.
                content_blocks = merged.get("content") or []
                # Concatenate text blocks into a single content string;
                # tool_use blocks become tool_calls.
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input") or {}),
                            },
                        })
                stop_reason = merged.get("stop_reason")
                message: dict[str, Any] = {
                    "role": "assistant",
                    "content": "".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    message["tool_calls"] = tool_calls
                synthesized_choice: dict[str, Any] = {
                    "index": 0,
                    "message": message,
                    "finish_reason": stop_reason if isinstance(stop_reason, str) else None,
                }
                record["gen_ai.response.choices"] = [synthesized_choice]
                if isinstance(stop_reason, str) and stop_reason:
                    record["gen_ai.response.finish_reasons"] = [stop_reason]
            usage = merged.get("usage")
            if isinstance(usage, dict):
                # OpenAI uses prompt_tokens/completion_tokens; Anthropic
                # uses input_tokens/output_tokens directly.
                if isinstance(usage.get("prompt_tokens"), int):
                    record["gen_ai.usage.input_tokens"] = usage["prompt_tokens"]
                elif isinstance(usage.get("input_tokens"), int):
                    record["gen_ai.usage.input_tokens"] = usage["input_tokens"]
                if isinstance(usage.get("completion_tokens"), int):
                    record["gen_ai.usage.output_tokens"] = usage["completion_tokens"]
                elif isinstance(usage.get("output_tokens"), int):
                    record["gen_ai.usage.output_tokens"] = usage["output_tokens"]
        else:
            # Non-streaming: try to parse the response body for richer
            # response-side fields. Failures fall back silently.
            try:
                resp_dict = json.loads(response_body) if response_body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                resp_dict = {}
            if isinstance(resp_dict, dict):
                response_id = resp_dict.get("id")
                if isinstance(response_id, str) and response_id:
                    record["gen_ai.response.id"] = response_id
                response_model = resp_dict.get("model")
                if isinstance(response_model, str) and response_model:
                    record["gen_ai.response.model"] = response_model

        if upstream_error:
            record["error"] = {
                "type": "UpstreamError",
                "message": upstream_error,
                "traceback_ref": None,
            }

        try:
            self._writer.append_model_call(record)
        except Exception:
            # Bookkeeping failure must never break the proxy.
            pass

    # ── Server loop ──────────────────────────────────────────────────────────

    def run(self) -> int:
        """Bind the listener and serve until shutdown signal.

        Returns the exit code (0 on clean shutdown). Blocks the calling
        thread.
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        proxy_self = self

        class _Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                # Convert headers to a plain dict (Message-like → dict[str,str]).
                hdrs = {str(k): str(v) for k, v in self.headers.items()}
                status, response_body, content_type = proxy_self._handle_request(
                    method, self.path, hdrs, body,
                )
                self.send_response(status)
                if content_type:
                    self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                if response_body:
                    self.wfile.write(response_body)

            def do_POST(self) -> None:  # noqa: N802 — stdlib API
                self._serve("POST")

            def do_GET(self) -> None:  # noqa: N802 — stdlib API
                self._serve("GET")

            def do_DELETE(self) -> None:  # noqa: N802 — stdlib API
                self._serve("DELETE")

            def do_PUT(self) -> None:  # noqa: N802 — stdlib API
                self._serve("PUT")

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
                # Silence stdlib's per-request stderr logging.
                return

        with ThreadingHTTPServer((self._listen_host, self._listen_port), _Handler) as srv:
            self._server = srv
            srv.serve_forever()
        return 0
