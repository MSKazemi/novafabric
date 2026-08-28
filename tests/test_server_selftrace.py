"""Tests for ADR-0182 D5 opt-in self-tracing (second slice).

Contract: design/spec/ops-observability-surface-v0.md §"Self-tracing"
  - default OFF: no emitter, no worker thread, /v0/version says false
  - enabled: OTLP/JSON-shaped HTTP request spans land at the configured
    LOCAL endpoint (the deployment's own OTLP ingest)
  - attribute privacy: no auth headers / raw path IDs / tenant markers in
    the exported payload bytes
  - no-phone-home: non-loopback endpoint hosts are refused unless
    NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE=1
  - bounded queue: overflow drops (counted) without ever blocking emit
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ObservabilityConfig, ServerConfig  # noqa: E402
from novafabric.server.observability import (  # noqa: E402
    DEFAULT_SELF_TRACE_ENDPOINT,
    SelfTraceEmitter,
    SelfTraceEndpointRefusedError,
    validate_self_trace_endpoint,
)

# --------------------------------------------------------------------------- #
# Local OTLP sink (the "deployment's own ingest" stand-in)
# --------------------------------------------------------------------------- #


class _SinkServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.received: list[dict[str, Any]] = []
        self.delay_s: float = 0.0
        self.received_lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/api/otlp/v1/traces"

    def bodies(self) -> bytes:
        with self.received_lock:
            return b"\n".join(r["body"] for r in self.received)


class _SinkHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        server: _SinkServer = self.server  # type: ignore[assignment]
        if server.delay_s:
            time.sleep(server.delay_s)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        with server.received_lock:
            server.received.append({"body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args: Any) -> None:  # silence test output
        pass


@pytest.fixture
def sink() -> Iterator[_SinkServer]:
    server = _SinkServer(("127.0.0.1", 0), _SinkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _wait_for(predicate: Any, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _traced_config(db_path: Path, endpoint: str) -> ServerConfig:
    return ServerConfig(
        db_path=str(db_path),
        insecure_no_auth=True,
        observability=ObservabilityConfig(
            self_tracing=True, self_tracing_endpoint=endpoint
        ),
    )


def _selftrace_threads() -> list[str]:
    return [
        t.name for t in threading.enumerate() if t.name.startswith("nova-self-trace")
    ]


def _close_emitter(app: Any) -> None:
    emitter = getattr(app.state, "self_trace_emitter", None)
    if emitter is not None:
        emitter.close()


# --------------------------------------------------------------------------- #
# Default OFF
# --------------------------------------------------------------------------- #


class TestDefaultOff:
    def test_no_emitter_no_thread_feature_false(self, tmp_path: Path) -> None:
        threads_before = set(_selftrace_threads())
        cfg = ServerConfig(db_path=str(tmp_path / "t.db"), insecure_no_auth=True)
        app = create_app(cfg)
        assert app.state.self_trace_emitter is None
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/livez")
        assert set(_selftrace_threads()) == threads_before  # no worker started
        body = client.get("/v0/version").json()
        assert body["features"]["self_tracing"] is False

    def test_config_defaults(self) -> None:
        obs = ObservabilityConfig()
        assert obs.self_tracing is False
        assert obs.self_tracing_endpoint is None

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_SELF_TRACING", "true")
        monkeypatch.setenv(
            "NOVAFABRIC_SERVER_SELF_TRACING_ENDPOINT", "http://127.0.0.1:9999/x"
        )
        cfg = ServerConfig()
        assert cfg.observability.self_tracing is True
        assert cfg.observability.self_tracing_endpoint == "http://127.0.0.1:9999/x"


# --------------------------------------------------------------------------- #
# Enabled: spans land at the local endpoint
# --------------------------------------------------------------------------- #


class TestEnabled:
    def test_http_request_span_lands_at_local_endpoint(
        self, tmp_path: Path, sink: _SinkServer
    ) -> None:
        app = create_app(_traced_config(tmp_path / "t.db", sink.url))
        client = TestClient(app, raise_server_exceptions=False)
        try:
            client.get("/livez")
            assert _wait_for(lambda: b"/livez" in sink.bodies()), (
                "no span reached the local OTLP sink"
            )
            payload = json.loads(sink.received[0]["body"])
            resource_spans = payload["resourceSpans"][0]
            res_attrs = {
                a["key"]: a["value"]
                for a in resource_spans["resource"]["attributes"]
            }
            assert res_attrs["service.name"]["stringValue"] == "nova-server"
            scope_spans = resource_spans["scopeSpans"][0]
            assert scope_spans["scope"]["name"] == "novafabric.self_trace"
            span = next(
                s for s in scope_spans["spans"] if s["name"] == "GET /livez"
            )
            attrs = {a["key"]: a["value"] for a in span["attributes"]}
            assert attrs["http.request.method"]["stringValue"] == "GET"
            assert attrs["http.route"]["stringValue"] == "/livez"
            assert attrs["http.response.status_code"]["intValue"] == "200"
            assert re.fullmatch(r"[0-9a-f]{32}", span["traceId"])
            assert re.fullmatch(r"[0-9a-f]{16}", span["spanId"])
            assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])
            assert span["kind"] == 2  # SPAN_KIND_SERVER
        finally:
            _close_emitter(app)

    def test_version_reports_self_tracing_enabled(
        self, tmp_path: Path, sink: _SinkServer
    ) -> None:
        app = create_app(_traced_config(tmp_path / "t.db", sink.url))
        client = TestClient(app, raise_server_exceptions=False)
        try:
            body = client.get("/v0/version").json()
            assert body["features"]["self_tracing"] is True
        finally:
            _close_emitter(app)

    def test_bearer_token_is_transport_only(
        self,
        tmp_path: Path,
        sink: _SinkServer,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The serve OTLP ingest is token-gated; the emitter may carry a bearer
        # token as a TRANSPORT header — it must never appear in the payload.
        monkeypatch.setenv("NOVAFABRIC_SELF_TRACE_TOKEN", "sst-transport-secret")
        app = create_app(_traced_config(tmp_path / "t.db", sink.url))
        client = TestClient(app, raise_server_exceptions=False)
        try:
            client.get("/livez")
            assert _wait_for(lambda: len(sink.received) >= 1)
            headers = sink.received[0]["headers"]
            assert headers.get("Authorization") == "Bearer sst-transport-secret"
            assert b"sst-transport-secret" not in sink.bodies()
        finally:
            _close_emitter(app)


# --------------------------------------------------------------------------- #
# Attribute privacy (normative)
# --------------------------------------------------------------------------- #


class TestAttributePrivacy:
    def test_no_auth_header_tenant_id_or_raw_path_in_payload(
        self, tmp_path: Path, sink: _SinkServer
    ) -> None:
        app = create_app(_traced_config(tmp_path / "t.db", sink.url))
        client = TestClient(app, raise_server_exceptions=False)
        try:
            client.get(
                "/v0/assets/raw-tenant-4242",
                headers={
                    "Authorization": "Bearer sst-super-secret-token",
                    "X-Tenant-Id": "tenant-9999",
                },
            )
            assert _wait_for(lambda: b"/v0/assets/" in sink.bodies())
            blob = sink.bodies()
            # Every assertion below is a NEGATIVE one, and a negative assertion
            # over an empty payload is vacuously true. Prove a span actually
            # arrived before concluding it is clean, so "nothing was emitted"
            # can never be mistaken for "nothing leaked".
            assert blob, "no self-trace payload emitted — privacy checks would be vacuous"
            assert b'"http.route"' in blob
            assert b"sst-super-secret-token" not in blob
            assert b"raw-tenant-4242" not in blob  # raw path segment
            assert b"tenant-9999" not in blob  # tenant header value
            assert b"/v0/assets/{asset_id}" in blob  # route template only
        finally:
            _close_emitter(app)

    def test_no_request_body_in_payload(
        self, tmp_path: Path, sink: _SinkServer
    ) -> None:
        app = create_app(_traced_config(tmp_path / "t.db", sink.url))
        client = TestClient(app, raise_server_exceptions=False)
        try:
            client.post(
                "/v0/orgs",
                json={"slug": "body-canary-org", "name": "Body Canary"},
            )
            assert _wait_for(lambda: b"/v0/orgs" in sink.bodies())
            blob = sink.bodies()
            # Same vacuity guard as above: an empty payload contains no body,
            # but that proves nothing about redaction.
            assert blob, "no self-trace payload emitted — privacy checks would be vacuous"
            assert b'"http.route"' in blob
            assert b"body-canary-org" not in blob
        finally:
            _close_emitter(app)


# --------------------------------------------------------------------------- #
# No-phone-home guard
# --------------------------------------------------------------------------- #


class TestNoPhoneHome:
    def test_non_loopback_host_refused(self) -> None:
        with pytest.raises(SelfTraceEndpointRefusedError):
            validate_self_trace_endpoint("http://collector.example.com:4318/v1/traces")
        with pytest.raises(SelfTraceEndpointRefusedError):
            validate_self_trace_endpoint("https://10.0.0.5:4318/v1/traces")

    def test_non_loopback_refused_at_app_construction(self, tmp_path: Path) -> None:
        cfg = _traced_config(
            tmp_path / "t.db", "http://collector.example.com:4318/v1/traces"
        )
        with pytest.raises(SelfTraceEndpointRefusedError):
            create_app(cfg)

    def test_allow_remote_env_permits_internal_collector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE", "1")
        url = "http://collector.internal:4318/v1/traces"
        assert validate_self_trace_endpoint(url) == url

    def test_loopback_hosts_accepted(self) -> None:
        for url in (
            "http://127.0.0.1:4321/api/otlp/v1/traces",
            "http://localhost:4321/api/otlp/v1/traces",
            "https://[::1]:4321/api/otlp/v1/traces",
        ):
            assert validate_self_trace_endpoint(url) == url

    def test_default_endpoint_is_loopback(self) -> None:
        assert validate_self_trace_endpoint(DEFAULT_SELF_TRACE_ENDPOINT)

    def test_non_http_scheme_refused(self) -> None:
        with pytest.raises(SelfTraceEndpointRefusedError):
            validate_self_trace_endpoint("ftp://127.0.0.1/traces")
        with pytest.raises(SelfTraceEndpointRefusedError):
            validate_self_trace_endpoint("not a url")


# --------------------------------------------------------------------------- #
# Bounded queue / fire-and-forget
# --------------------------------------------------------------------------- #


class TestBoundedQueue:
    def test_overflow_drops_without_blocking(self, sink: _SinkServer) -> None:
        sink.delay_s = 0.5  # slow consumer: the worker wedges on each POST
        emitter = SelfTraceEmitter(
            sink.url, "server", queue_size=2, batch_max=1
        )
        try:
            start = time.monotonic()
            for _ in range(50):
                emitter.emit_http_span(
                    route="/livez",
                    method="GET",
                    status_code=200,
                    start_time_ns=1,
                    end_time_ns=2,
                )
            elapsed = time.monotonic() - start
            assert elapsed < 0.5, "emit_http_span must never block on a full queue"
            assert emitter.dropped_total >= 1
        finally:
            emitter.close()

    def test_unreachable_endpoint_counts_drops_and_stays_quiet(self) -> None:
        # A closed loopback port: sends fail, spans are counted as dropped,
        # nothing raises into the caller.
        emitter = SelfTraceEmitter(
            "http://127.0.0.1:9/api/otlp/v1/traces", "server", timeout_s=0.2
        )
        try:
            emitter.emit_http_span(
                route="/livez",
                method="GET",
                status_code=200,
                start_time_ns=1,
                end_time_ns=2,
            )
            assert _wait_for(lambda: emitter.dropped_total >= 1)
            assert emitter.sent_total == 0
        finally:
            emitter.close()

    def test_sent_total_counts_accepted_spans(self, sink: _SinkServer) -> None:
        emitter = SelfTraceEmitter(sink.url, "server")
        try:
            emitter.emit_http_span(
                route="/livez",
                method="GET",
                status_code=200,
                start_time_ns=1,
                end_time_ns=2,
            )
            assert _wait_for(lambda: emitter.sent_total == 1)
            assert emitter.dropped_total == 0
        finally:
            emitter.close()
