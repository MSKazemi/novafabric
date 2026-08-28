"""Storage-quota enforcement for the server app (ADR-0179 second slice, experimental).

Second slice of the contract in ``the private design/spec/rate-limiting-quotas-v0.md``
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
AlertHook = Callable[["QuotaViolation"], None]


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
    """One limit at/over capacity: which kind, the usage, and the limit hit.

    ``workspace`` (ADR-0208, additive) is set only for per-workspace budget
    violations; global violations keep it None and are byte-identical.
    """

    kind: str  # "capsules" | "bytes"
    usage: int
    limit: int
    severity: Severity
    workspace: str | None = None


@dataclass(frozen=True)
class QuotaDecision:
    """Outcome of one quota check (``ok`` | ``warn`` | ``reject``)."""

    outcome: Outcome
    violations: tuple[QuotaViolation, ...] = ()

    @property
    def warning_header(self) -> str:
        """``X-NovaFabric-Quota-Warning`` value: ``<kind> <usage>/<limit>``.

        Workspace violations render ``<workspace>/<kind> <usage>/<limit>``
        (ADR-0208 D3); mixed decisions comma-join, global parts first.
        """
        return ", ".join(
            (f"{v.workspace}/" if v.workspace else "") + f"{v.kind} {v.usage}/{v.limit}"
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
        alert_hook: AlertHook | None = None,
    ) -> None:
        self._quota = quota
        self._usage_provider = usage_provider or _default_usage_provider
        self._clock = clock
        self.cache_ttl = float(cache_ttl)
        self._audit_window = float(audit_window_seconds)
        self._audit_hook = audit_hook or emit_quota_audit
        self._alert_hook = alert_hook or _emit_quota_breach_alert
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
        if violation.severity == "hard":
            # ADR-0192 wired source: ops.quota.breached from the hard rejection
            # path. Fail-safe and bounded: rides this method's audit window, is
            # wrapped so it can never raise, and (when alerting is configured)
            # delivers on a background thread so the 429 is never slowed.
            try:
                self._alert_hook(violation)
            except Exception:  # noqa: BLE001 — alerting must never break requests
                logger.warning("quota breach alert emission failed", exc_info=True)


class WorkspaceQuotaChecker:
    """Per-workspace warn-then-reject checks over metered counters (ADR-0208 D3).

    The same ladder and contract as :class:`QuotaChecker`, one budget per
    workspace slug. Usage for enforcement is the **all-time metered**
    ``capsules_created`` / ``bytes_stored`` sums for the workspace (negative
    delete adjustments included) — not the FS walk, which cannot attribute —
    read through the short-TTL :class:`~novafabric.server.usage.
    WorkspaceUsageReader` cache. A workspace without a configured budget (or
    with all-zero limits) is unlimited and never queries usage.

    Audit + alert bounding: one audit event per (severity, workspace, kind)
    per audit window; the soft threshold additionally emits one
    ``warning``-severity ``ops.quota.breached`` per window, and the hard path
    a ``critical`` one with ``subject_ref = "quota:{workspace}:{kind}"`` —
    per-workspace subjects, so one team's breach never suppresses another's
    page in the ADR-0192 dedup window.
    """

    def __init__(
        self,
        budgets: dict[str, Any],
        usage_reader: Callable[[str], tuple[int, int]],
        *,
        clock: Callable[[], float] = time.monotonic,
        audit_window_seconds: float = 60.0,
        audit_hook: AuditHook | None = None,
        alert_hook: AlertHook | None = None,
        soft_alert_hook: AlertHook | None = None,
        invalidate_hook: Callable[[str | None], None] | None = None,
    ) -> None:
        # budgets: slug -> WorkspaceQuotaConfig (typed Any to avoid a config
        # import cycle; only the four max_* attributes are read).
        self._budgets = {
            slug: b for slug, b in budgets.items() if getattr(b, "any_limit", False)
        }
        self._usage_reader = usage_reader
        self._clock = clock
        self._audit_window = float(audit_window_seconds)
        self._audit_hook = audit_hook or emit_quota_audit
        self._alert_hook = alert_hook or _emit_workspace_breach_alert
        self._soft_alert_hook = soft_alert_hook or _emit_workspace_soft_alert
        self._invalidate_hook = invalidate_hook
        # (event, workspace, kind) -> (window_start_monotonic, window_start_utc)
        self._audit_windows: dict[tuple[str, str, str], tuple[float, str]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """True when at least one workspace budget has a non-zero limit."""
        return bool(self._budgets)

    def invalidate(self, workspace: str | None = None) -> None:
        """Drop the cached usage for *workspace* (or all) — post-write hook."""
        if self._invalidate_hook is not None:
            self._invalidate_hook(workspace)

    def check(self, workspace: str) -> QuotaDecision:
        """Warn-then-reject decision for one workspace (``0`` = unlimited)."""
        budget = self._budgets.get(workspace)
        if budget is None:
            return QuotaDecision(outcome="ok")
        capsules, total_bytes = self._usage_reader(workspace)
        violations: list[QuotaViolation] = []
        for kind, used, soft, hard in (
            (KIND_CAPSULES, capsules, budget.max_capsules_soft, budget.max_capsules_hard),
            (KIND_BYTES, total_bytes, budget.max_bytes_soft, budget.max_bytes_hard),
        ):
            if hard and used >= hard:
                violations.append(
                    QuotaViolation(
                        kind=kind, usage=used, limit=hard,
                        severity="hard", workspace=workspace,
                    )
                )
            elif soft and used >= soft:
                violations.append(
                    QuotaViolation(
                        kind=kind, usage=used, limit=soft,
                        severity="soft", workspace=workspace,
                    )
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
        """One audit event (+ one alert) per (severity, workspace, kind) window."""
        event = EVENT_HARD if violation.severity == "hard" else EVENT_SOFT
        key = (event, violation.workspace or "", violation.kind)
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
            # Additive field (spec §Audit): quota audit gains workspace scope.
            "workspace": violation.workspace,
            "window_start": window_start_utc,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self._audit_hook(payload)
        except Exception:  # noqa: BLE001 — auditing must never break requests
            logger.warning("workspace quota audit emission failed", exc_info=True)
        hook = self._alert_hook if violation.severity == "hard" else self._soft_alert_hook
        try:
            hook(violation)
        except Exception:  # noqa: BLE001 — alerting must never break requests
            logger.warning("workspace quota alert emission failed", exc_info=True)


def _emit_workspace_breach_alert(violation: QuotaViolation) -> None:
    """`ops.quota.breached` (critical) for one workspace hard rejection.

    ADR-0208/ADR-0192: ``subject_ref = "quota:{workspace}:{kind}"`` keys the
    alert-layer dedup per workspace. Refs and counts only (ADR-0137 hygiene);
    no-op unless ``NOVA_ALERTS_*`` is configured; never raises;
    ``background=True`` keeps delivery off the 429 path.
    """
    from novafabric.events.alerts import emit_ops_alert  # noqa: PLC0415

    emit_ops_alert(
        event_type="ops.quota.breached",
        severity="critical",
        subject_ref=f"quota:{violation.workspace}:{violation.kind}",
        payload={
            "workspace": violation.workspace,
            "kind": violation.kind,
            "usage": violation.usage,
            "limit": violation.limit,
        },
        source="nova server",
        background=True,
    )


def _emit_workspace_soft_alert(violation: QuotaViolation) -> None:
    """`ops.quota.breached` (warning) when a workspace soft threshold crosses.

    Bounded by the checker's audit window (at most one per (workspace, kind)
    per window — spec §Alert thresholds); subject carries a ``:soft`` suffix
    so warning and critical dedup independently.
    """
    from novafabric.events.alerts import emit_ops_alert  # noqa: PLC0415

    emit_ops_alert(
        event_type="ops.quota.breached",
        severity="warning",
        subject_ref=f"quota:{violation.workspace}:{violation.kind}:soft",
        payload={
            "workspace": violation.workspace,
            "kind": violation.kind,
            "usage": violation.usage,
            "limit": violation.limit,
        },
        source="nova server",
        background=True,
    )


def _emit_quota_breach_alert(violation: QuotaViolation) -> None:
    """Emit `ops.quota.breached` (ADR-0192) for one hard storage-quota rejection.

    Byte-identical no-op unless the user configured a ``NOVA_ALERTS_*``
    endpoint; ``emit_ops_alert`` never raises, and ``background=True`` keeps
    endpoint latency off the quota decision path.
    """
    from novafabric.events.alerts import emit_ops_alert  # noqa: PLC0415

    emit_ops_alert(
        event_type="ops.quota.breached",
        severity="critical",
        subject_ref=f"quota:{violation.kind}",
        payload={
            "kind": violation.kind,
            "usage": violation.usage,
            "limit": violation.limit,
        },
        source="nova server",
        background=True,
    )


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
    details: dict[str, Any] = {"kind": v.kind, "usage": v.usage, "limit": v.limit}
    if v.workspace is not None:
        # Additive field (ADR-0208 D3): workspace budget rejections name the
        # workspace; global rejections keep the exact pre-0208 details shape.
        details["workspace"] = v.workspace
    return error_response(429, "quota_exceeded", str(exc), details)


async def enforce_storage_quota(request: Request, response: Response) -> None:
    """FastAPI dependency guarding the capsule-ingest write routes.

    Inert when no checker is installed (feature disabled / all limits zero).
    Global (ADR-0179) and workspace (ADR-0208) checks both run; the
    strictest outcome wins. Soft-limit breach sets the warning header on the
    (successful) response — global parts first, comma-joined; hard-limit
    breach raises :class:`QuotaExceededError` → 429.
    """
    checker: QuotaChecker | None = getattr(request.app.state, "quota_checker", None)
    ws_checker: WorkspaceQuotaChecker | None = getattr(
        request.app.state, "workspace_quota_checker", None
    )
    decisions: list[QuotaDecision] = []
    if checker is not None:
        decisions.append(checker.check())
    if ws_checker is not None:
        # Attribution rides the auth context resolved earlier in the
        # dependency chain (require_role precedes this dependency).
        from novafabric.server import usage as usage_mod  # noqa: PLC0415

        config = request.app.state.config
        attribution = usage_mod.resolve_attribution(
            getattr(request.state, "auth", None),
            Path(config.db_path) if config.db_path else None,
        )
        decisions.append(ws_checker.check(attribution.workspace))
    if not decisions:
        return
    for decision in decisions:
        if decision.outcome == "reject":
            hard = decision.hard
            if hard is not None:
                raise QuotaExceededError(hard)
    warning = ", ".join(
        d.warning_header for d in decisions if d.outcome == "warn"
    )
    if warning:
        response.headers[QUOTA_WARNING_HEADER] = warning


def install_quota_enforcement(app: FastAPI, config: ServerConfig) -> None:
    """Install the quota checkers when the ADR-0179 quota track is active.

    Requires ``server.rate_limits.enabled`` (the spec's master switch gates
    quotas too) AND a ``quota`` block; the global checker installs when a
    global limit is non-zero (unchanged ADR-0179 path), the workspace checker
    (ADR-0208) when the additive ``quota.workspaces`` map holds at least one
    non-zero budget. Otherwise nothing is installed and the ingest routes'
    dependency is a no-op — zero behavior change.
    """
    app.add_exception_handler(QuotaExceededError, quota_exceeded_handler)  # type: ignore[arg-type]
    rl = config.rate_limits
    if not rl.enabled or rl.quota is None:
        return
    checker = QuotaChecker(rl.quota, audit_window_seconds=rl.audit_window_seconds)
    if checker.enabled:
        app.state.quota_checker = checker
        logger.info(
            "storage quotas enabled (ADR-0179): capsules soft=%s hard=%s, "
            "bytes soft=%s hard=%s",
            rl.quota.max_capsules_soft,
            rl.quota.max_capsules_hard,
            rl.quota.max_bytes_soft,
            rl.quota.max_bytes_hard,
        )
    if rl.quota.workspaces:
        from novafabric.server.usage import WorkspaceUsageReader  # noqa: PLC0415

        reader = WorkspaceUsageReader(
            Path(config.db_path) if config.db_path else None
        )
        ws_checker = WorkspaceQuotaChecker(
            dict(rl.quota.workspaces),
            reader.get,
            audit_window_seconds=rl.audit_window_seconds,
            invalidate_hook=reader.invalidate,
        )
        if ws_checker.enabled:
            app.state.workspace_quota_checker = ws_checker
            logger.info(
                "per-workspace quota budgets enabled (ADR-0208, experimental):"
                " %d workspace(s)",
                len(rl.quota.workspaces),
            )
