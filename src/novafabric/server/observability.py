"""Self-observability surface for the NovaFabric HTTP apps (ADR-0182, experimental).

Implements the contract in ``design/spec/ops-observability-surface-v0.md``:

- ``/livez``   — process liveness only; never checks dependencies.
- ``/readyz``  — itemized dependency readiness (db, migrations, object_store);
  503 when any required check fails, each check named in the JSON body.
- ``/v0/version`` — structured build/deployment identity, role-gated (reader).
- ``/metrics`` — Prometheus text exposition; gated by default on the server app
  (operator may exempt via config), behind the existing token on the serve app.

Second slice (ADR-0182 D4/D5):

- remaining metric inventory — ``nova_ingest_events_total`` (server ingest
  write routes), ``nova_readyz_check_status`` (0/1 per readiness check), and
  the ``nova_db_pool_in_use`` / ``nova_db_pool_size`` gauges (registered but
  honestly sample-less today: neither backend keeps a connection pool — the
  postgres path opens per-request ``psycopg.connect`` connections);
- **opt-in self-tracing** (:class:`SelfTraceEmitter`) — default OFF; when
  enabled, one OTLP/JSON-shaped span per HTTP request is POSTed fire-and-forget
  to the deployment's *own* OTLP ingest (serve's ``POST /api/otlp/v1/traces``)
  through a bounded queue with a drop counter. Never blocks requests, never
  retries, and NORMATIVELY never records request bodies, auth headers, or
  tenant identifiers in span attributes. Non-loopback endpoint hosts are
  refused unless ``NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE=1`` — this is explicitly
  **not telemetry**; spans never leave the deployment (no-phone-home,
  ``design/strategy/non-goals.md`` reaffirmed).

Normative rules honoured here (spec §"Privacy and cardinality rules"):

- the ``route`` metric label is always the registered route **template**
  (``/v0/runs/{id}``), never the raw request path; unmatched paths collapse to
  the single value ``"unmatched"``;
- no tenant, workspace, or user identifiers ever appear in metric labels;
- metrics use a dedicated ``CollectorRegistry`` per app instance — never the
  prometheus-client global default registry — so repeated ``create_app()``
  calls (tests, embedding) cannot trip duplicate-timeseries registration.

``prometheus-client`` ships in the ``[server]`` extra and is imported lazily;
without it the apps still work and ``/metrics`` answers 503 with a clear
install hint.

This module keeps its import-time dependencies to FastAPI + stdlib so the
``[serve]`` extra (no alembic, no psycopg, no PyJWT) can reuse the helpers;
anything heavier is imported lazily inside functions.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import ipaddress
import logging
import os
import queue
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from novafabric.server.config import ServerConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Check-status vocabulary (spec: "ok" | "fail" | "skipped"; "unknown" is the
# honest degradation when a check is not determinable cheaply — it never fails
# readiness).
# ---------------------------------------------------------------------------

CHECK_OK = "ok"
CHECK_FAIL = "fail"
CHECK_SKIPPED = "skipped"
CHECK_UNKNOWN = "unknown"

#: Single collapsed route label for requests that matched no registered route.
UNMATCHED_ROUTE = "unmatched"

#: Bounded outcome vocabulary for ``nova_ingest_events_total`` (spec D4).
INGEST_OUTCOMES = ("accepted", "rejected")

#: Server-app ingest write routes → bounded ``encoding`` label values.
#: The server app's data-plane ingest is the capsule ZIP upload and the
#: external score submission (ADR-0119); the OTLP json/protobuf ingest lives
#: on the serve app. Keys are ``(method, route_template)``.
SERVER_INGEST_ROUTES: Mapping[tuple[str, str], str] = {
    ("POST", "/v0/capsules"): "zip",
    ("POST", "/v0/capsules/{run_id}/scores"): "json",
}

ReadinessCheck = Callable[[], str]


class MetricsUnavailableError(Exception):
    """Raised when ``/metrics`` support is requested but prometheus-client is absent."""


_PROMETHEUS_INSTALL_HINT = (
    "prometheus-client is not installed — /metrics requires the [server] "
    "extra: pip install 'novafabric[server]'"
)


# ---------------------------------------------------------------------------
# HTTP metrics (dedicated registry — never the global default)
# ---------------------------------------------------------------------------


class HttpMetrics:
    """HTTP request counter + duration histogram on a dedicated registry.

    Labels are bounded per the spec: ``app`` (server|serve), ``route``
    (registered route template only), ``method``, ``status``.
    """

    def __init__(self, app_name: str) -> None:
        try:
            from prometheus_client import (  # noqa: PLC0415
                CollectorRegistry,
                Counter,
                Gauge,
                Histogram,
            )
        except ImportError as exc:
            raise MetricsUnavailableError(_PROMETHEUS_INSTALL_HINT) from exc

        self.app_name = app_name
        # Dedicated registry: the KG pipeline's global-registry duplicate
        # registration pain (kg/pipeline.py) is deliberately avoided here.
        self.registry = CollectorRegistry()
        self._requests_total = Counter(
            "nova_http_requests_total",
            "HTTP requests handled, by route template / method / status.",
            ["app", "route", "method", "status"],
            registry=self.registry,
        )
        self._request_duration = Histogram(
            "nova_http_request_duration_seconds",
            "HTTP request handling duration in seconds, by route template / method.",
            ["app", "route", "method"],
            registry=self.registry,
        )
        # ---- second-slice inventory (ADR-0182 D4) --------------------------
        self._ingest_events = Counter(
            "nova_ingest_events_total",
            "Ingest events processed on the app's ingest write routes, "
            "by encoding / outcome.",
            ["app", "encoding", "outcome"],
            registry=self.registry,
        )
        self._readyz_check = Gauge(
            "nova_readyz_check_status",
            "Readiness check status: 1 = passing (ok/skipped/unknown), 0 = failing.",
            ["app", "check"],
            registry=self.registry,
        )
        # DB pool gauges. Registered so the inventory is discoverable, but they
        # carry samples only when a pooled backend exists. Today neither
        # backend pools: sqlite has no pool at all and the postgres path opens
        # per-request psycopg connections — so no samples is the honest state.
        self._db_pool_in_use = Gauge(
            "nova_db_pool_in_use",
            "DB pool connections currently in use, by pool name. No samples "
            "when the configured backend keeps no connection pool.",
            ["app", "pool"],
            registry=self.registry,
        )
        self._db_pool_size = Gauge(
            "nova_db_pool_size",
            "DB pool configured size, by pool name. No samples when the "
            "configured backend keeps no connection pool.",
            ["app", "pool"],
            registry=self.registry,
        )

    def observe(self, route: str, method: str, status: int, duration_s: float) -> None:
        """Record one handled request. *route* MUST be a route template."""
        self._requests_total.labels(
            app=self.app_name, route=route, method=method, status=str(status)
        ).inc()
        self._request_duration.labels(
            app=self.app_name, route=route, method=method
        ).observe(duration_s)

    def observe_ingest(self, encoding: str, outcome: str, count: int = 1) -> None:
        """Count *count* ingest events. Labels MUST stay bounded (spec D4)."""
        self._ingest_events.labels(
            app=self.app_name, encoding=encoding, outcome=outcome
        ).inc(count)

    def ensure_ingest_labels(self, encodings: Iterable[str]) -> None:
        """Pre-initialise ingest counter children at 0 so /metrics shows them."""
        for encoding in encodings:
            for outcome in INGEST_OUTCOMES:
                self._ingest_events.labels(
                    app=self.app_name, encoding=encoding, outcome=outcome
                )

    def set_readyz_check(self, check: str, passing: bool) -> None:
        """Set the 0/1 readiness gauge for one named check."""
        self._readyz_check.labels(app=self.app_name, check=check).set(
            1 if passing else 0
        )

    def set_db_pool(self, pool: str, in_use: int, size: int) -> None:
        """Report pool occupancy — call ONLY when a real connection pool exists."""
        self._db_pool_in_use.labels(app=self.app_name, pool=pool).set(in_use)
        self._db_pool_size.labels(app=self.app_name, pool=pool).set(size)

    def render(self) -> tuple[bytes, str]:
        """Return ``(payload, content_type)`` in Prometheus text exposition format."""
        from prometheus_client import (  # noqa: PLC0415
            CONTENT_TYPE_LATEST,
            generate_latest,
        )

        return generate_latest(self.registry), CONTENT_TYPE_LATEST


def maybe_http_metrics(app_name: str) -> HttpMetrics | None:
    """Build :class:`HttpMetrics`, or return None (with a warning) if unavailable."""
    try:
        return HttpMetrics(app_name)
    except MetricsUnavailableError as exc:
        logger.warning("HTTP metrics disabled: %s", exc)
        return None


def install_http_metrics_middleware(
    app: FastAPI,
    metrics: HttpMetrics,
    ingest_routes: Mapping[tuple[str, str], str] | None = None,
) -> None:
    """Attach the request counter/duration middleware to *app*.

    The route label is resolved from the matched route object *after* routing,
    so it is always the registered template — never the raw path.

    *ingest_routes* maps ``(method, route_template)`` to a bounded ``encoding``
    label value; matching requests additionally count one
    ``nova_ingest_events_total`` event (``accepted`` on 2xx/3xx, ``rejected``
    otherwise).
    """

    @app.middleware("http")
    async def _http_metrics_middleware(  # type: ignore[no-untyped-def]
        request: Request, call_next
    ):
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_s = time.perf_counter() - start
            route = request.scope.get("route")
            template = getattr(route, "path", None) or UNMATCHED_ROUTE
            try:
                metrics.observe(template, request.method, status_code, duration_s)
                if ingest_routes:
                    encoding = ingest_routes.get((request.method, template))
                    if encoding is not None:
                        outcome = "accepted" if status_code < 400 else "rejected"
                        metrics.observe_ingest(encoding, outcome)
            except Exception:  # noqa: BLE001 — metrics must never break requests
                logger.debug("metrics observation failed", exc_info=True)


def metrics_response(metrics: HttpMetrics | None) -> Response:
    """Render the /metrics response body (503 + hint if metrics are unavailable)."""
    if metrics is None:
        return JSONResponse(
            status_code=503,
            content={
                "error": "metrics_unavailable",
                "detail": _PROMETHEUS_INSTALL_HINT,
            },
        )
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


# ---------------------------------------------------------------------------
# Self-tracing (ADR-0182 D5 — opt-in, default OFF, never telemetry)
# ---------------------------------------------------------------------------

#: Default destination: the deployment's own serve OTLP ingest (ADR-0177) on
#: the `nova serve` default port. Loopback by construction — no phone-home.
DEFAULT_SELF_TRACE_ENDPOINT = "http://127.0.0.1:4321/api/otlp/v1/traces"

#: Env var that (set to "1") permits a non-loopback self-tracing endpoint —
#: for operator-configured *deployment-internal* collectors only.
SELF_TRACE_ALLOW_REMOTE_ENV = "NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE"

#: Optional bearer token for the OTLP POSTs (serve's ingest is token-gated).
#: Transport credential only — it NEVER appears in span attributes or logs.
SELF_TRACE_TOKEN_ENV = "NOVAFABRIC_SELF_TRACE_TOKEN"

_SPAN_KIND_SERVER = 2  # opentelemetry.proto.trace.v1.Span.SpanKind.SPAN_KIND_SERVER
_STATUS_CODE_UNSET = 0
_STATUS_CODE_ERROR = 2

_SELF_TRACE_SENTINEL: Any = object()


class SelfTraceEndpointRefusedError(ValueError):
    """Self-tracing endpoint rejected by the no-phone-home guard (ADR-0182 D5)."""


def _is_loopback_host(host: str) -> bool:
    """True for loopback hosts (127.0.0.0/8, ::1, 'localhost')."""
    name = host.strip().lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def validate_self_trace_endpoint(endpoint: str) -> str:
    """Enforce the no-phone-home guarantee on a self-tracing endpoint URL.

    Only ``http``/``https`` URLs are accepted, and the host MUST be loopback
    unless the operator explicitly sets ``NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE=1``
    for a deployment-internal collector. Raises
    :class:`SelfTraceEndpointRefusedError` (loudly, at startup — never a
    silent drop) on violation; returns *endpoint* unchanged when acceptable.
    """
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in ("http", "https"):
        raise SelfTraceEndpointRefusedError(
            f"self-tracing endpoint must be an http(s) URL, got scheme "
            f"{parsed.scheme!r} (ADR-0182 D5)"
        )
    host = parsed.hostname or ""
    if not host:
        raise SelfTraceEndpointRefusedError(
            "self-tracing endpoint URL has no host (ADR-0182 D5)"
        )
    if not _is_loopback_host(host) and os.environ.get(
        SELF_TRACE_ALLOW_REMOTE_ENV
    ) != "1":
        raise SelfTraceEndpointRefusedError(
            f"refusing non-loopback self-tracing endpoint host {host!r}: spans "
            f"must stay inside the deployment (ADR-0182 D5, no phone-home). "
            f"Set {SELF_TRACE_ALLOW_REMOTE_ENV}=1 only for a "
            f"deployment-internal collector."
        )
    return endpoint


class SelfTraceEmitter:
    """Minimal in-process OTLP/JSON span emitter (ADR-0182 D5, experimental).

    Deliberately NOT the OpenTelemetry SDK exporter pipeline: this emitter is
    ~a bounded ``queue.Queue`` plus one daemon thread that POSTs
    ``ExportTraceServiceRequest``-shaped JSON batches to the configured
    endpoint. Properties:

    - **fire-and-forget** — ``emit_http_span`` never blocks: a full queue
      increments :attr:`dropped_total` and returns;
    - **no retries** — a failed or non-2xx POST drops the batch and counts it;
    - **fail-open** — nothing here can break request handling;
    - **attribute privacy by construction** — spans carry ONLY the route
      template, method, status code, and timing. No request bodies, no auth
      headers, no tenant/workspace/user identifiers (normative, spec §self-tracing).
    """

    def __init__(
        self,
        endpoint: str,
        app_name: str,
        *,
        token: str | None = None,
        queue_size: int = 1024,
        batch_max: int = 64,
        timeout_s: float = 2.0,
    ) -> None:
        self.endpoint = validate_self_trace_endpoint(endpoint)
        self.app_name = app_name
        self._token = token
        self._batch_max = max(1, batch_max)
        self._timeout_s = timeout_s
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._lock = threading.Lock()
        self._dropped = 0
        self._sent = 0
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"nova-self-trace-{app_name}", daemon=True
        )
        self._thread.start()

    @property
    def dropped_total(self) -> int:
        """Spans dropped so far (queue overflow + failed exports)."""
        with self._lock:
            return self._dropped

    @property
    def sent_total(self) -> int:
        """Spans accepted by the endpoint so far."""
        with self._lock:
            return self._sent

    def emit_http_span(
        self,
        *,
        route: str,
        method: str,
        status_code: int,
        start_time_ns: int,
        end_time_ns: int,
    ) -> None:
        """Queue one HTTP server span. Never blocks; overflow is counted."""
        span = {
            "traceId": os.urandom(16).hex(),
            "spanId": os.urandom(8).hex(),
            "name": f"{method} {route}",
            "kind": _SPAN_KIND_SERVER,
            "startTimeUnixNano": str(start_time_ns),
            "endTimeUnixNano": str(end_time_ns),
            # NORMATIVE (spec §self-tracing): route template / method / status
            # ONLY — never bodies, auth headers, or tenant identifiers.
            "attributes": [
                {"key": "http.request.method", "value": {"stringValue": method}},
                {"key": "http.route", "value": {"stringValue": route}},
                {
                    "key": "http.response.status_code",
                    "value": {"intValue": str(status_code)},
                },
            ],
            "status": {
                "code": _STATUS_CODE_ERROR
                if status_code >= 500
                else _STATUS_CODE_UNSET
            },
        }
        try:
            self._queue.put_nowait(span)
        except queue.Full:
            self._note_dropped(1)

    def close(self, timeout_s: float = 3.0) -> None:
        """Stop the worker thread (tests / graceful shutdown). Idempotent."""
        self._closed.set()
        try:
            self._queue.put_nowait(_SELF_TRACE_SENTINEL)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout_s)

    # ------------------------------------------------------------------ #

    def _note_dropped(self, count: int) -> None:
        with self._lock:
            self._dropped += count

    def _export_payload(self, spans: list[dict[str, Any]]) -> dict[str, Any]:
        """OTLP/HTTP JSON ``ExportTraceServiceRequest`` shape (ADR-0177)."""
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": f"nova-{self.app_name}"},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {
                                "name": "novafabric.self_trace",
                                "version": _package_version(),
                            },
                            "spans": spans,
                        }
                    ],
                }
            ]
        }

    def _run(self) -> None:
        import httpx  # noqa: PLC0415 — core dep, imported off the hot path

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        with httpx.Client(timeout=self._timeout_s) as client:
            while True:
                try:
                    item = self._queue.get(timeout=0.2)
                except queue.Empty:
                    if self._closed.is_set():
                        return
                    continue
                if item is _SELF_TRACE_SENTINEL:
                    return
                batch: list[dict[str, Any]] = [item]
                stop = False
                while len(batch) < self._batch_max:
                    try:
                        nxt = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is _SELF_TRACE_SENTINEL:
                        stop = True
                        break
                    batch.append(nxt)
                try:
                    resp = client.post(
                        self.endpoint,
                        json=self._export_payload(batch),
                        headers=headers,
                    )
                except Exception:  # noqa: BLE001 — fire-and-forget, no retry
                    self._note_dropped(len(batch))
                else:
                    if resp.status_code >= 400:
                        self._note_dropped(len(batch))
                    else:
                        with self._lock:
                            self._sent += len(batch)
                if stop or self._closed.is_set():
                    return


def install_self_tracing_middleware(app: FastAPI, emitter: SelfTraceEmitter) -> None:
    """Attach the one-span-per-HTTP-request middleware to *app*.

    Route resolution mirrors the metrics middleware: the span name/attribute
    carries the registered route **template** (or ``"unmatched"``), never the
    raw path. Emission is fail-open and non-blocking.
    """

    @app.middleware("http")
    async def _self_tracing_middleware(  # type: ignore[no-untyped-def]
        request: Request, call_next
    ):
        start_ns = time.time_ns()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", None) or UNMATCHED_ROUTE
            try:
                emitter.emit_http_span(
                    route=template,
                    method=request.method,
                    status_code=status_code,
                    start_time_ns=start_ns,
                    end_time_ns=time.time_ns(),
                )
            except Exception:  # noqa: BLE001 — tracing must never break requests
                logger.debug("self-trace emission failed", exc_info=True)


# ---------------------------------------------------------------------------
# Liveness / readiness
# ---------------------------------------------------------------------------


def livez_payload() -> dict[str, str]:
    """Process liveness body. MUST stay dependency-free (spec: /livez)."""
    return {"status": "ok"}


def readiness_response(
    checks: Mapping[str, ReadinessCheck],
    metrics: HttpMetrics | None = None,
) -> JSONResponse:
    """Run itemized readiness *checks* and build the /readyz response.

    200 ``{"status": "ok"}`` when no check fails; 503 ``{"status": "degraded"}``
    when any check reports ``"fail"``. ``"skipped"`` and ``"unknown"`` never
    fail readiness. The body contains only check names and statuses — never
    DSNs, hostnames, or error strings (spec normative rule).

    When *metrics* is given, each check also sets its
    ``nova_readyz_check_status`` gauge (1 = passing, 0 = failing).
    """
    results: dict[str, str] = {}
    for name, check in checks.items():
        try:
            results[name] = check()
        except Exception:  # noqa: BLE001 — a raising check is a failing check
            logger.warning("readiness check %r raised", name, exc_info=True)
            results[name] = CHECK_FAIL
    if metrics is not None:
        try:
            for name, status in results.items():
                metrics.set_readyz_check(name, status != CHECK_FAIL)
        except Exception:  # noqa: BLE001 — metrics must never break the probe
            logger.debug("readyz gauge update failed", exc_info=True)
    degraded = any(v == CHECK_FAIL for v in results.values())
    return JSONResponse(
        status_code=503 if degraded else 200,
        content={"status": "degraded" if degraded else "ok", "checks": results},
    )


def check_sqlite_db(db_path: Path | None) -> str:
    """Cheap SELECT 1 against the SQLite registry database."""
    try:
        path = db_path or _default_db_path()
        conn = sqlite3.connect(path, timeout=2)
        try:
            conn.execute("SELECT 1")
        finally:
            conn.close()
        return CHECK_OK
    except Exception:  # noqa: BLE001
        return CHECK_FAIL


def check_postgres_db(dsn: str | None) -> str:
    """Cheap SELECT 1 against the configured Postgres backend."""
    if not dsn:
        return CHECK_FAIL
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        return CHECK_UNKNOWN
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return CHECK_OK
    except Exception:  # noqa: BLE001
        return CHECK_FAIL


def _skew_status_to_check(status: str) -> str:
    """Map a schema-skew comparator status onto the readiness vocabulary.

    ``behind`` and ``ahead_or_foreign`` are real, named failures;
    ``unstamped``/``unknown`` stay the honest ``"unknown"`` — never a fake
    ``"ok"`` and never a refusal based on ignorance (ADR-0211 D1).
    """
    from novafabric.server import schema_skew  # noqa: PLC0415

    if status == schema_skew.STATUS_OK:
        return CHECK_OK
    if status in (schema_skew.STATUS_BEHIND, schema_skew.STATUS_AHEAD):
        return CHECK_FAIL
    return CHECK_UNKNOWN


def check_migrations_sqlite(db_path: Path | None) -> str:
    """Compare the SQLite ``alembic_version`` stamp against the script head.

    ADR-0211 D1: delegates to the shared ``schema_skew`` comparator (packaged
    migration trees, source-checkout fallback). Honest degradation: an
    unstamped or unreadable DB reports ``"unknown"`` — never a fake ``"ok"``.
    """
    from novafabric.server.schema_skew import compare_revisions  # noqa: PLC0415

    try:
        path = db_path or _default_db_path()
    except Exception:  # noqa: BLE001 — home resolution failure
        return CHECK_UNKNOWN
    return _skew_status_to_check(compare_revisions("sqlite", path).status)


def check_migrations_postgres(dsn: str | None) -> str:
    """Compare the Postgres ``alembic_version`` stamp against the script head.

    ADR-0211 D1: this replaces the previous hardcoded ``"unknown"`` — the
    comparator opens one short-timeout connection (DSN never logged) and
    resolves the packaged registry postgres track head.
    """
    from novafabric.server.schema_skew import compare_revisions  # noqa: PLC0415

    if not dsn:
        return CHECK_UNKNOWN
    return _skew_status_to_check(compare_revisions("postgres", dsn).status)


def _default_db_path() -> Path:
    from novafabric.registry.store import get_db_path  # noqa: PLC0415

    return get_db_path()


# ---------------------------------------------------------------------------
# /v0/version payload
# ---------------------------------------------------------------------------


def build_version_payload(
    *,
    schema_revision: str,
    features: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Structured build/deployment identity per the spec's /v0/version table."""
    return {
        "version": _package_version(),
        "git_sha": detect_git_sha(),
        "schema_revision": schema_revision,
        "extras": detect_extras(),
        "features": features or {},
    }


def _package_version() -> str:
    try:
        return importlib_metadata.version("novafabric")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_git_sha_cache: str | None = None


def detect_git_sha() -> str:
    """Git SHA of the running build.

    Resolution order: ``NOVA_BUILD_SHA`` env var (set by build/deploy
    pipelines), then ``git rev-parse HEAD`` when running from a source
    checkout; ``"unknown"`` otherwise (spec: never fabricate).
    """
    env_sha = os.environ.get("NOVA_BUILD_SHA", "").strip()
    if env_sha:
        return env_sha
    global _git_sha_cache  # noqa: PLW0603
    if _git_sha_cache is not None:
        return _git_sha_cache
    sha = "unknown"
    try:
        proc = subprocess.run(  # noqa: S603, S607 — fixed argv, no user input
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        candidate = proc.stdout.strip()
        if proc.returncode == 0 and _GIT_SHA_RE.fullmatch(candidate):
            sha = candidate
    except Exception:  # noqa: BLE001 — git absent, timeout, sandbox…
        sha = "unknown"
    _git_sha_cache = sha
    return sha


def detect_schema_revision(db_path: Path | None) -> str:
    """Schema revision applied to the metadata DB.

    Prefers the alembic stamp (``alembic_version.version_num``); falls back to
    the registry's integer ``schema_version``; ``"unknown"`` when neither is
    readable.
    """
    try:
        path = db_path or _default_db_path()
        if not Path(path).exists():
            return "unknown"
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            try:
                row = conn.execute(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    return str(row[0])
            except sqlite3.OperationalError:
                pass
            try:
                row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                if row and row[0] is not None:
                    return str(row[0])
            except sqlite3.OperationalError:
                pass
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return "unknown"
    return "unknown"


_REQUIREMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_EXTRA_MARKER_RE = re.compile(r"""extra\s*==\s*['"]([^'"]+)['"]""")


def detect_extras(package: str = "novafabric") -> list[str]:
    """Optional extras whose declared dependencies are all installed.

    Derived from package metadata (Provides-Extra + requirement markers), so
    it needs no hardcoded extra→module map and stays current as extras evolve.
    """
    try:
        md = importlib_metadata.metadata(package)
        requires = importlib_metadata.requires(package) or []
    except importlib_metadata.PackageNotFoundError:
        return []
    declared = set(md.get_all("Provides-Extra") or [])
    by_extra: dict[str, list[str]] = {}
    for req in requires:
        if ";" not in req:
            continue
        body, marker = req.split(";", 1)
        match = _EXTRA_MARKER_RE.search(marker)
        if not match or match.group(1) not in declared:
            continue
        name_match = _REQUIREMENT_NAME_RE.match(body.strip())
        if name_match:
            by_extra.setdefault(match.group(1), []).append(name_match.group(0))
    enabled = [
        extra
        for extra, dists in sorted(by_extra.items())
        if dists and all(_dist_installed(d) for d in dists)
    ]
    return enabled


def _dist_installed(name: str) -> bool:
    try:
        importlib_metadata.version(name)
        return True
    except importlib_metadata.PackageNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Server-app wiring
# ---------------------------------------------------------------------------


def install_server_observability(app: FastAPI, config: ServerConfig) -> None:
    """Register /livez, /readyz, /v0/version, and /metrics on the server app.

    ``/health`` (registered by the app factory) stays untouched as the
    compatibility alias. RBAC imports happen here — not at module import — so
    the serve app can reuse this module's helpers on a lean ``[serve]`` install.
    """
    from novafabric.server.rbac import Role, require_role  # noqa: PLC0415

    metrics = maybe_http_metrics("server")
    if metrics is not None:
        app.state.http_metrics = metrics
        metrics.ensure_ingest_labels(set(SERVER_INGEST_ROUTES.values()))
        install_http_metrics_middleware(
            app, metrics, ingest_routes=SERVER_INGEST_ROUTES
        )
        # DB pool gauges: registered (family visible in /metrics) but never
        # set — neither backend keeps a connection pool today (sqlite: none;
        # postgres: per-request psycopg.connect). Call metrics.set_db_pool()
        # from the pool's lifecycle when a pooled backend lands.

    # Self-tracing (ADR-0182 D5): opt-in, default OFF. When enabled, one span
    # per HTTP request is exported into the deployment's own OTLP ingest —
    # never an external endpoint (validate_self_trace_endpoint enforces the
    # no-phone-home guard loudly at startup).
    app.state.self_trace_emitter = None
    if config.observability.self_tracing:
        emitter = SelfTraceEmitter(
            endpoint=(
                config.observability.self_tracing_endpoint
                or DEFAULT_SELF_TRACE_ENDPOINT
            ),
            app_name="server",
            token=os.environ.get(SELF_TRACE_TOKEN_ENV),
        )
        app.state.self_trace_emitter = emitter
        install_self_tracing_middleware(app, emitter)

    db_path = Path(config.db_path) if config.db_path else None

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, str]:
        return livez_payload()

    def _db_check() -> str:
        if config.backend == "postgres":
            return check_postgres_db(config.postgres_dsn)
        return check_sqlite_db(db_path)

    def _migrations_check() -> str:
        if config.backend == "postgres":
            # ADR-0211 D1: real head comparison via the shared comparator
            # (bounded connect_timeout; DSN never logged).
            return check_migrations_postgres(config.postgres_dsn)
        return check_migrations_sqlite(db_path)

    checks: dict[str, ReadinessCheck] = {
        "db": _db_check,
        "migrations": _migrations_check,
        # The server app has no object-store configuration surface yet; the
        # spec mandates checking it ONLY when configured — so: skipped.
        "object_store": lambda: CHECK_SKIPPED,
    }

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        return readiness_response(checks, metrics=metrics)

    @app.get("/v0/version", dependencies=[Depends(require_role(Role.reader))])
    async def version() -> dict[str, Any]:
        return build_version_payload(
            schema_revision=(
                CHECK_UNKNOWN
                if config.backend == "postgres"
                else detect_schema_revision(db_path)
            ),
            features={
                # ADR-0182 D5 — the real config value (opt-in, default OFF).
                "self_tracing": config.observability.self_tracing,
                "metrics": metrics is not None,
                "oidc": config.oidc.enabled,
                "scim": config.scim_active,
            },
        )

    # Gated by default (ADR-0182 D1); the operator exemption is applied at
    # registration time because the config is fixed for the app's lifetime.
    metrics_dependencies = (
        [] if config.observability.metrics_exempt else [Depends(require_role(Role.reader))]
    )

    @app.get("/metrics", include_in_schema=False, dependencies=metrics_dependencies)
    async def metrics_endpoint() -> Response:
        return metrics_response(metrics)
