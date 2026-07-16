"""In-process token-bucket rate limiter for the server app (ADR-0179, experimental).

First slice of the contract in ``design/spec/rate-limiting-quotas-v0.md``:

- stdlib-only token buckets on a **monotonic clock**, held in a **bounded**
  in-process map (LRU eviction) — deliberately not distributed, because the
  supported topology is single-writer (ADR-0179);
- keyed per the spec's strict fallback order: authenticated **principal**
  (bearer-token id) → **tenant** → **client IP**;
- three limit classes — ``ingest`` (write verbs + OTLP ingest paths),
  ``read`` (GET/HEAD), ``admin`` (``/v0/admin`` + ``/v0/roles``) — each with
  its own configurable ``rate``/``burst``;
- **disabled by default** (``server.rate_limits.enabled: false`` ⇒ the
  middleware is never installed — zero behavior change);
- ``/health``, ``/livez``, ``/readyz``, ``/metrics`` are **never** limited and
  carry no ``X-RateLimit-*`` headers;
- a limited request gets ``429`` with the ADR-0017 error envelope
  (``error.code = "rate_limited"``), ``Retry-After`` in delta-seconds (ceil),
  and ``X-RateLimit-Limit`` / ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset``;
  non-limited responses on classed routes carry the three ``X-RateLimit-*``
  headers too (spec: "every response on a route belonging to a limit class");
- sustained limiting of one key (>= ``audit_threshold_rejections`` rejections
  within ``audit_window_seconds``) emits **one audit record per key per
  window** via the house append-only audit log — the raw key value is never
  stored, only a SHA-256 digest.

The storage-quota track of ADR-0179 (warn-then-reject at capsule ingest)
lives in the sibling module ``novafabric.server.quotas``.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from fastapi import FastAPI, Request, Response

if TYPE_CHECKING:
    from novafabric.server.config import RateLimitsConfig, ServerConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

CLASS_INGEST = "ingest"
CLASS_READ = "read"
CLASS_ADMIN = "admin"

KEY_PRINCIPAL = "principal"
KEY_TENANT = "tenant"
KEY_CLIENT_IP = "client_ip"

#: Paths that MUST never be rate limited (spec "Exemptions"): probes must not
#: brown out with the API, and /metrics is how operators see the limiting.
EXEMPT_PATHS = frozenset({"/health", "/livez", "/readyz", "/metrics"})

#: Route prefixes belonging to the ``admin`` limit class.  The role-management
#: surface (ADR-0060) is mounted under /v0/admin; /v0/roles is reserved for
#: the future top-level mount (ADR-0178 org/workspace surface).
_ADMIN_PREFIXES = ("/v0/admin", "/v0/roles")

#: Verbs classified as reads when the path is not otherwise classified.
_READ_METHODS = frozenset({"GET", "HEAD"})

#: Default bound on the bucket map (LRU eviction above this).
DEFAULT_MAX_BUCKETS = 10_000

_RATE_LIMITED_MESSAGE = (
    "Rate limit exceeded for the {limit_class!r} class; retry after {retry_after}s."
)


def classify_route(method: str, path: str) -> str | None:
    """Map a request to its limit class, or ``None`` when exempt.

    Order: exemptions → admin prefixes → OTLP ingest paths → write verbs
    (ingest) → reads.
    """
    if path in EXEMPT_PATHS:
        return None
    if any(path == p or path.startswith(p + "/") for p in _ADMIN_PREFIXES):
        return CLASS_ADMIN
    if "otlp" in path:
        # OTLP ingest endpoints are ingest-class regardless of verb (spec).
        return CLASS_INGEST
    if method.upper() in _READ_METHODS:
        return CLASS_READ
    return CLASS_INGEST


def resolve_key_parts(
    authorization: str | None,
    tenant_id: str | None,
    client_ip: str | None,
) -> tuple[str, str]:
    """Resolve the bucket key ``(key_type, key_value)`` per the spec.

    Strict fallback order — first available wins:

    1. **principal** — the bearer-token id (SHA-256 of the presented token;
       resolvable before auth runs, and two principals never share a bucket);
    2. **tenant** — a resolved tenant id (no server surface resolves one
       before routing today; the slot exists so ADR-0178 can populate it);
    3. **client IP** — unauthenticated surfaces.

    The raw token never becomes the key value — only its digest.
    """
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            return KEY_PRINCIPAL, hashlib.sha256(token.encode()).hexdigest()
    if tenant_id:
        return KEY_TENANT, tenant_id
    return KEY_CLIENT_IP, client_ip or "unknown"


def _resolve_key(request: Request) -> tuple[str, str]:
    """Resolve the bucket key from a live request (see :func:`resolve_key_parts`)."""
    return resolve_key_parts(
        request.headers.get("Authorization"),
        getattr(request.state, "tenant_id", None),
        request.client.host if request.client else None,
    )


def key_hash(key_value: str) -> str:
    """``sha256:<64hex>`` digest of a key value — the audit-safe form."""
    return f"sha256:{hashlib.sha256(key_value.encode()).hexdigest()}"


# ---------------------------------------------------------------------------
# Token buckets
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    """Outcome of one limiter check, carrying the response-header values."""

    allowed: bool
    limit_class: str
    limit: int  # X-RateLimit-Limit — the class burst (bucket capacity)
    remaining: int  # X-RateLimit-Remaining — whole tokens still available
    reset_seconds: int  # X-RateLimit-Reset — delta-seconds until bucket full
    retry_after: int | None = None  # Retry-After — only on denials


@dataclass
class _Entry:
    """Per-key bucket state plus the sustained-rejection audit window."""

    tokens: float
    last: float
    rejections: int = 0
    window_start_mono: float | None = None
    window_start_utc: str = ""
    audited: bool = False


AuditHook = Callable[[dict[str, Any]], None]


class TokenBucketLimiter:
    """Bounded map of token buckets keyed by ``(key_type, key_value, class)``.

    Thread-safe (single lock); the clock is injectable for tests and MUST be
    monotonic. Classes never share buckets; the map is LRU-bounded.
    """

    def __init__(
        self,
        config: RateLimitsConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        audit_hook: AuditHook | None = None,
        max_buckets: int = DEFAULT_MAX_BUCKETS,
    ) -> None:
        self._config = config
        self._clock = clock
        self._audit_hook = audit_hook or emit_sustained_limiting_audit
        self._max_buckets = max_buckets
        self._buckets: OrderedDict[tuple[str, str, str], _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._buckets)

    def _class_budget(self, limit_class: str) -> tuple[float, int]:
        cls = getattr(self._config, limit_class)
        return float(cls.rate), int(cls.burst)

    def check(self, key_type: str, key_value: str, limit_class: str) -> Decision:
        """Refill, then try to take one token for this key/class."""
        rate, burst = self._class_budget(limit_class)
        now = self._clock()
        with self._lock:
            key = (key_type, key_value, limit_class)
            entry = self._buckets.get(key)
            if entry is None:
                entry = _Entry(tokens=float(burst), last=now)
                self._buckets[key] = entry
                if len(self._buckets) > self._max_buckets:
                    self._buckets.popitem(last=False)  # evict least-recent
            else:
                self._buckets.move_to_end(key)
                entry.tokens = min(float(burst), entry.tokens + (now - entry.last) * rate)
                entry.last = now

            if entry.tokens >= 1.0:
                entry.tokens -= 1.0
                return Decision(
                    allowed=True,
                    limit_class=limit_class,
                    limit=burst,
                    remaining=max(0, math.floor(entry.tokens)),
                    reset_seconds=self._reset_seconds(entry.tokens, rate, burst),
                )

            retry_after = max(1, math.ceil((1.0 - entry.tokens) / rate))
            self._record_rejection(entry, key_type, key_value, limit_class, now)
            return Decision(
                allowed=False,
                limit_class=limit_class,
                limit=burst,
                remaining=0,
                reset_seconds=self._reset_seconds(entry.tokens, rate, burst),
                retry_after=retry_after,
            )

    @staticmethod
    def _reset_seconds(tokens: float, rate: float, burst: int) -> int:
        return max(0, math.ceil((burst - tokens) / rate))

    def _record_rejection(
        self,
        entry: _Entry,
        key_type: str,
        key_value: str,
        limit_class: str,
        now: float,
    ) -> None:
        """Sustained-limiting accounting; emits at most one audit per window."""
        window = float(self._config.audit_window_seconds)
        if entry.window_start_mono is None or (now - entry.window_start_mono) > window:
            entry.window_start_mono = now
            entry.window_start_utc = datetime.now(timezone.utc).isoformat()
            entry.rejections = 0
            entry.audited = False
        entry.rejections += 1
        if entry.audited or entry.rejections < self._config.audit_threshold_rejections:
            return
        entry.audited = True
        payload: dict[str, Any] = {
            "event": "rate_limit_sustained",
            "key_type": key_type,
            "key_hash": key_hash(key_value),
            "limit_class": limit_class,
            "rejected_count": entry.rejections,
            "window_start": entry.window_start_utc,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._audit_hook(payload)
        except Exception:  # noqa: BLE001 — auditing must never break requests
            logger.warning("rate-limit audit emission failed", exc_info=True)


def emit_sustained_limiting_audit(payload: dict[str, Any]) -> None:
    """Append the sustained-limiting record to the house audit log.

    Uses the same append-only JSONL audit log the server's role-management
    routes already write to (``novafabric.serve.audit``). Note: that log is
    append-only but **not** hash-chained; the hash-chained ``AuditLog``
    (``novafabric.audit``) is not yet wired into the server app — the spec's
    hash-chaining requirement lands with that wiring (see ADR-0179 status).
    A structured warning is always logged for operator visibility.
    """
    logger.warning(
        "sustained rate limiting: key=%s class=%s rejected=%s window_start=%s",
        payload.get("key_hash"),
        payload.get("limit_class"),
        payload.get("rejected_count"),
        payload.get("window_start"),
    )
    from novafabric.serve import audit  # noqa: PLC0415

    digest = str(payload.get("key_hash", ""))
    audit.append(
        action="rate_limit_sustained",
        args=payload,
        cli_equivalent="n/a (server rate-limit middleware, ADR-0179)",
        actor_token_fp=digest.removeprefix("sha256:")[:8] or "unknown",
        result="ok",
    )


# ---------------------------------------------------------------------------
# Middleware wiring
# ---------------------------------------------------------------------------


def _apply_headers(response: Response, decision: Decision) -> None:
    response.headers["X-RateLimit-Limit"] = str(decision.limit)
    response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
    response.headers["X-RateLimit-Reset"] = str(decision.reset_seconds)


def install_rate_limiting(app: FastAPI, config: ServerConfig) -> None:
    """Install the rate-limit middleware when ``server.rate_limits.enabled``.

    Disabled (the default) ⇒ nothing is installed — zero behavior change, no
    headers, no timing side effects.

    Ordering note: Starlette wraps the last-added middleware **outermost**, so
    this must be installed *before* ``install_server_observability`` adds the
    HTTP-metrics middleware — that keeps metrics outside the limiter and makes
    429 rejections visible in ``nova_http_requests_total``.
    """
    rl = config.rate_limits
    if not rl.enabled:
        return

    limiter = TokenBucketLimiter(rl)
    app.state.rate_limiter = limiter
    logger.info(
        "rate limiting enabled (ADR-0179): ingest=%s/%s read=%s/%s admin=%s/%s",
        rl.ingest.rate,
        rl.ingest.burst,
        rl.read.rate,
        rl.read.burst,
        rl.admin.rate,
        rl.admin.burst,
    )

    @app.middleware("http")
    async def _rate_limit_middleware(  # type: ignore[no-untyped-def]
        request: Request, call_next
    ):
        limit_class = classify_route(request.method, request.url.path)
        if limit_class is None:
            # Exempt routes consume no tokens and carry no X-RateLimit-* headers.
            return await call_next(request)

        key_type, key_value = _resolve_key(request)
        decision = limiter.check(key_type, key_value, limit_class)

        if not decision.allowed:
            from novafabric.server.errors import error_response  # noqa: PLC0415

            retry_after = decision.retry_after or 1
            response: Response = error_response(
                429,
                "rate_limited",
                _RATE_LIMITED_MESSAGE.format(
                    limit_class=limit_class, retry_after=retry_after
                ),
                {"limit_class": limit_class},
            )
            response.headers["Retry-After"] = str(retry_after)
            _apply_headers(response, decision)
            return response

        response = await call_next(request)
        _apply_headers(response, decision)
        return response
