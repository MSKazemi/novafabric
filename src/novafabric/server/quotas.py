"""Storage-quota enforcement for the server app (ADR-0179 second slice, experimental).

Second slice of the contract in ``design/spec/rate-limiting-quotas-v0.md``
(the rate-limit track shipped first — see ``rate_limit.py``):

- usage is **derived from the existing capsule store** (capsule count, total
  stored bytes) — no new ledger table of truth to reconcile;
- checked at **ingest only** (the capsule write routes), with
  **warn-then-reject** semantics:

  * at/over a **soft** limit ⇒ the write succeeds and the response carries an
    ``X-NovaFabric-Quota-Warning: <kind> <usage>/<limit>`` header, plus one
    audit event per kind per audit window;
  * at/over a **hard** limit ⇒ the write is rejected ``429`` with the
    ADR-0017 envelope (``error.code = "quota_exceeded"``, ``details`` carrying
    ``kind``/``usage``/``limit``) and **no** ``Retry-After`` header — quota
    does not decay on a clock (spec);

- ``0`` means **unlimited** and short-circuits without querying the store;
  the whole feature is inert unless ``server.rate_limits.enabled`` is true
  AND a ``quota`` block with at least one non-zero limit is configured —
  upgrading changes zero behavior;
- the derivation query is cached with a short **monotonic TTL**
  (:data:`DEFAULT_CACHE_TTL_SECONDS`) so hot ingest paths don't re-count the
  store on every request (spec: cacheable count with bounded staleness);
- audit events (``quota_soft_exceeded`` / ``quota_hard_exceeded``) reuse the
  rate-limit throttle pattern — at most one per kind per
  ``audit_window_seconds`` — and go to the same append-only audit log.

Scope is per-deployment in this slice; per-workspace scoping is deferred to
an ADR-0178 follow-on (see ADR-0179 status).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from novafabric.server.config import QuotaConfig, ServerConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Warning header carried by writes that succeeded over a soft limit (spec).
QUOTA_WARNING_HEADER = "X-NovaFabric-Quota-Warning"

KIND_CAPSULES = "capsules"
KIND_BYTES = "bytes"

EVENT_SOFT = "quota_soft_exceeded"
EVENT_HARD = "quota_hard_exceeded"

#: Bounded staleness of the derived usage counts (spec: the derivation query
#: cost at ingest must be bounded; a cacheable count is acceptable).
DEFAULT_CACHE_TTL_SECONDS = 5.0

Outcome = Literal["ok", "warn", "reject"]
Severity = Literal["soft", "hard"]

_QUOTA_EXCEEDED_MESSAGE = (
    "Storage quota exceeded: {kind} usage {usage} has reached the hard limit "
    "{limit}. Delete or archive capsules, or raise the quota."
)


# ---------------------------------------------------------------------------
# Usage measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaUsage:
    """Derived storage usage: capsule count + total stored bytes."""

    capsules: int
    total_bytes: int


UsageProvider = Callable[[], QuotaUsage]
AuditHook = Callable[[dict[str, Any]], None]


def measure_capsule_store(capsule_dir: Path) -> QuotaUsage:
    """Derive usage from the existing capsule store (no new ledger).

    Capsule count = immediate subdirectories holding a ``capsule.yaml``
    (the same definition the list route uses); total bytes = every file
    under the store. Unreadable entries are skipped, never fatal.
    """
    if not capsule_dir.is_dir():
        return QuotaUsage(capsules=0, total_bytes=0)
    capsules = 0
    total_bytes = 0
    try:
        for entry in capsule_dir.iterdir():
            if entry.is_dir() and (entry / "capsule.yaml").exists():
                capsules += 1
    except OSError:  # pragma: no cover — store vanished mid-scan
        return QuotaUsage(capsules=0, total_bytes=0)
    for root, _dirs, files in os.walk(capsule_dir):
        for name in files:
            try:
                total_bytes += os.path.getsize(os.path.join(root, name))
            except OSError:  # pragma: no cover — file vanished mid-scan
                continue
    return QuotaUsage(capsules=capsules, total_bytes=total_bytes)


def _default_usage_provider() -> QuotaUsage:
    """Measure the default capsule store (env-resolved lazily per call)."""
    from novafabric._paths import default_capsule_dir  # noqa: PLC0415

    return measure_capsule_store(default_capsule_dir())


# ---------------------------------------------------------------------------
# Decision model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaViolation:
    """One limit at/over capacity: which kind, the usage, and the limit hit."""

    kind: str  # "capsules" | "bytes"
    usage: int
    limit: int
    severity: Severity


@dataclass(frozen=True)
class QuotaDecision:
    """Outcome of one quota check (``ok`` | ``warn`` | ``reject``)."""

    outcome: Outcome
    violations: tuple[QuotaViolation, ...] = ()

    @property
    def warning_header(self) -> str:
        """``X-NovaFabric-Quota-Warning`` value: ``<kind> <usage>/<limit>``."""
        return ", ".join(
            f"{v.kind} {v.usage}/{v.limit}"
            for v in self.violations
            if v.severity == "soft"
        )

    @property
    def hard(self) -> QuotaViolation | None:
        """The first hard violation, when the outcome is ``reject``."""
        for v in self.violations:
            if v.severity == "hard":
                return v
        return None


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class QuotaChecker:
    """Warn-then-reject storage-quota checks over TTL-cached store counts.

    Thread-safe (single lock); the clock is injectable for tests and MUST be
    monotonic. ``0`` limits are unlimited; when every limit is ``0`` the
    checker is disabled and :meth:`check` never queries the store.
    """

    def __init__(
        self,
        quota: QuotaConfig | None,
        usage_provider: UsageProvider | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        cache_ttl: float = DEFAULT_CACHE_TTL_SECONDS,
        audit_window_seconds: float = 60.0,
        audit_hook: AuditHook | None = None,
    ) -> None:
        self._quota = quota
        self._usage_provider = usage_provider or _default_usage_provider
        self._clock = clock
        self.cache_ttl = float(cache_ttl)
        self._audit_window = float(audit_window_seconds)
        self._audit_hook = audit_hook or emit_quota_audit
        self._cached: QuotaUsage | None = None
        self._cached_at: float | None = None
        # (event, kind) -> (window_start_monotonic, window_start_utc)
        self._audit_windows: dict[tuple[str, str], tuple[float, str]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """True when a quota block with at least one non-zero limit exists."""
        q = self._quota
        return q is not None and any(
            (
                q.max_capsules_soft,
                q.max_capsules_hard,
                q.max_bytes_soft,
                q.max_bytes_hard,
            )
        )

    def invalidate(self) -> None:
        """Drop the cached usage so the next check re-counts the store."""
        with self._lock:
            self._cached = None
            self._cached_at = None

    def _usage(self) -> QuotaUsage:
        now = self._clock()
        with self._lock:
            if (
                self._cached is not None
                and self._cached_at is not None
                and (now - self._cached_at) < self.cache_ttl
            ):
                return self._cached
            usage = self._usage_provider()
            self._cached = usage
            self._cached_at = now
            return usage

    def check(self) -> QuotaDecision:
        """Compare current usage against the limits (``0`` = unlimited).

        A limit triggers when usage has **reached** it (``usage >= limit``):
        the store already holds its full allowance, so the incoming write
        would exceed capacity. Disabled ⇒ ``ok`` without querying the store.
        """
        if not self.enabled:
            return QuotaDecision(outcome="ok")
        q = self._quota
        if q is None:  # pragma: no cover — enabled implies a quota block
            return QuotaDecision(outcome="ok")
        usage = self._usage()
        violations: list[QuotaViolation] = []
        for kind, used, soft, hard in (
            (KIND_CAPSULES, usage.capsules, q.max_capsules_soft, q.max_capsules_hard),
            (KIND_BYTES, usage.total_bytes, q.max_bytes_soft, q.max_bytes_hard),
        ):
            if hard and used >= hard:
                violations.append(
                    QuotaViolation(kind=kind, usage=used, limit=hard, severity="hard")
                )
            elif soft and used >= soft:
                violations.append(
                    QuotaViolation(kind=kind, usage=used, limit=soft, severity="soft")
                )
        if not violations:
            return QuotaDecision(outcome="ok")
        outcome: Outcome = (
            "reject" if any(v.severity == "hard" for v in violations) else "warn"
        )
        for violation in violations:
            self._maybe_audit(violation)
        return QuotaDecision(outcome=outcome, violations=tuple(violations))

    def _maybe_audit(self, violation: QuotaViolation) -> None:
        """Emit at most one audit event per (event, kind) per audit window."""
        event = EVENT_HARD if violation.severity == "hard" else EVENT_SOFT
        key = (event, violation.kind)
        now = self._clock()
        with self._lock:
            window = self._audit_windows.get(key)
            if window is not None and (now - window[0]) <= self._audit_window:
                return
            window_start_utc = datetime.now(timezone.utc).isoformat()
            self._audit_windows[key] = (now, window_start_utc)
        payload: dict[str, Any] = {
            "event": event,
            "kind": violation.kind,
            "usage": violation.usage,
            "limit": violation.limit,
            "window_start": window_start_utc,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._audit_hook(payload)
        except Exception:  # noqa: BLE001 — auditing must never break requests
            logger.warning("quota audit emission failed", exc_info=True)


def emit_quota_audit(payload: dict[str, Any]) -> None:
    """Append the quota event to the house audit log.

    Same append-only JSONL log the rate-limit track writes to
    (``novafabric.serve.audit``); a structured warning is always logged for
    operator visibility. Quota events carry no key hash — scope is
    per-deployment in this slice (spec: ``limit_class`` absent for quota
    events).
    """
    logger.warning(
        "storage quota %s: kind=%s usage=%s limit=%s window_start=%s",
        payload.get("event"),
        payload.get("kind"),
        payload.get("usage"),
        payload.get("limit"),
        payload.get("window_start"),
    )
    from novafabric.serve import audit  # noqa: PLC0415

    audit.append(
        action=str(payload.get("event", "quota_event")),
        args=payload,
        cli_equivalent="n/a (server storage-quota enforcement, ADR-0179)",
        actor_token_fp="quota",
        result="ok",
    )


# ---------------------------------------------------------------------------
# FastAPI wiring
# ---------------------------------------------------------------------------


class QuotaExceededError(Exception):
    """A write hit a hard storage-quota limit (429, ``quota_exceeded``)."""

    def __init__(self, violation: QuotaViolation) -> None:
        super().__init__(
            _QUOTA_EXCEEDED_MESSAGE.format(
                kind=violation.kind, usage=violation.usage, limit=violation.limit
            )
        )
        self.violation = violation


async def quota_exceeded_handler(
    request: Request, exc: QuotaExceededError
) -> JSONResponse:
    """429 with the ADR-0017 envelope; ``Retry-After`` deliberately omitted
    (quota does not decay on a clock — spec)."""
    from novafabric.server.errors import error_response  # noqa: PLC0415

    v = exc.violation
    return error_response(
        429,
        "quota_exceeded",
        str(exc),
        {"kind": v.kind, "usage": v.usage, "limit": v.limit},
    )


async def enforce_storage_quota(request: Request, response: Response) -> None:
    """FastAPI dependency guarding the capsule-ingest write routes.

    Inert when no checker is installed (feature disabled / all limits zero).
    Soft-limit breach sets the warning header on the (successful) response;
    hard-limit breach raises :class:`QuotaExceededError` → 429.
    """
    checker: QuotaChecker | None = getattr(request.app.state, "quota_checker", None)
    if checker is None:
        return
    decision = checker.check()
    if decision.outcome == "reject":
        hard = decision.hard
        if hard is not None:
            raise QuotaExceededError(hard)
    elif decision.outcome == "warn":
        response.headers[QUOTA_WARNING_HEADER] = decision.warning_header


def install_quota_enforcement(app: FastAPI, config: ServerConfig) -> None:
    """Install the quota checker when the ADR-0179 quota track is active.

    Requires ``server.rate_limits.enabled`` (the spec's master switch gates
    quotas too) AND a ``quota`` block with at least one non-zero limit;
    otherwise nothing is installed and the ingest routes' dependency is a
    no-op — zero behavior change.
    """
    app.add_exception_handler(QuotaExceededError, quota_exceeded_handler)  # type: ignore[arg-type]
    rl = config.rate_limits
    if not rl.enabled or rl.quota is None:
        return
    checker = QuotaChecker(rl.quota, audit_window_seconds=rl.audit_window_seconds)
    if not checker.enabled:
        return  # all limits zero ⇒ unlimited ⇒ stay inert
    app.state.quota_checker = checker
    logger.info(
        "storage quotas enabled (ADR-0179): capsules soft=%s hard=%s, "
        "bytes soft=%s hard=%s",
        rl.quota.max_capsules_soft,
        rl.quota.max_capsules_hard,
        rl.quota.max_bytes_soft,
        rl.quota.max_bytes_hard,
    )
