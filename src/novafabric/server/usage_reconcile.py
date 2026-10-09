"""Usage-ledger reconciliation (ADR-0208 P2, experimental).

``nova server usage reconcile`` compares the **metered lifetime** sums
(``usage_ledger`` → counters/rollups **plus** the retention carry in
``usage_pruned_totals`` — :func:`usage.lifetime_totals`) with the **derived**
global usage of the capsule store (``measure_capsule_store``, the global
source of truth) and reports the drift between them.

Same-window rule: the capsule store is measured over its whole history, so
the metered side must be too. The enforcement figure
(:func:`usage.all_time_totals`, which ``GET /v0/usage`` also uses for its
``drift`` block) is a rolling ``rollup_retention_months`` window — comparing
it against the whole store would report every monthly rollup prune as fresh
positive drift, and ``--apply`` would re-book it as a billable
current-period row every month. The lifetime figure keeps pruned totals, so
retention pruning is drift-neutral.

Contract (ADR-0208 "Reconciliation", spec ``usage-metering-v0.md``):

- **Report-only by default.** A run without ``apply=True`` writes nothing to
  the ledger; it only appends one ``usage.reconcile`` entry to the
  hash-chained audit log (spec §Audit: "reconciliation runs … append to the
  audit trail").
- **Explicit adjustment.** With ``apply=True`` and a non-zero drift, one
  signed (positive *or* negative ``amount``) adjustment row per drifting
  dimension (``capsules_created`` / ``bytes_stored``) is **appended** to the
  ledger with ``attribution = 'reconciliation'`` and
  ``ref = 'recon:<UTC timestamp>'``, via
  :func:`usage.record_entries_in_transaction` — ledger and counter move in
  one SQLite transaction and the adjustment is idempotent under the
  ``(metric, ref)`` unique index. Existing rows and counters are never
  rewritten; the counter changes only because a new, visible ledger row
  says so.
- **Serialized apply.** The apply path scans the store, then takes the
  SQLite write lock (``BEGIN IMMEDIATE``), reads the metered side and
  appends the adjustment inside that one transaction. A second concurrent
  ``--apply`` blocks on the lock and, once it acquires it, reads the
  already-adjusted ledger and finds nothing left to apply — two runs never
  book the same drift twice.
- **Default workspace only.** Pre-metering or unattributed bytes cannot be
  attributed to a real workspace without guessing, which the ADR refuses —
  so there is deliberately no workspace selector. A negative adjustment that
  would drive the default workspace's metered total below zero is refused
  (:class:`ReconciliationRefusedError`): the excess belongs to some other
  workspace and naming it would be a guess.

"Signed", stated honestly: the adjustment ``amount`` is signed (±); the run
itself is recorded in the **unkeyed** SHA-256 hash-chained audit log
(:class:`novafabric.audit.AuditLog` — tamper-*evident*, not a keyed
signature; keyed signing is NovaSeal's layer, ADR-0030). The ledger schema
carries no signature column and this slice adds none.

Remaining race, stated honestly: the lock serializes **metering** writers,
not the filesystem, and the store scan runs before the lock (a long
``os.walk`` must not stall upload metering). An upload whose capsule and
ledger row land on opposite sides of the scan/lock boundary is booked as
drift (±1 capsule) while its own metering row also stands; the next
reconcile reports the opposite drift and can reverse it. Bounded to uploads
in flight during the scan; never a repeat of the same drift by two
reconcile runs.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field

from novafabric.server import usage
from novafabric.server.quotas import QuotaUsage, measure_capsule_store

logger = logging.getLogger(__name__)

#: ``attribution`` value of adjustment rows (allowed by the spec's CHECK).
ATTRIBUTION_RECONCILIATION = "reconciliation"

#: Prefix of the adjustment rows' ``ref`` (spec: ``recon:<timestamp>``).
RECON_REF_PREFIX = "recon:"


class ReconciliationError(Exception):
    """Base class for reconciliation failures."""


class ReconciliationRefusedError(ReconciliationError):
    """The adjustment would require guessing an attribution — refused."""


class ReconciliationAuditError(ReconciliationError):
    """The adjustment committed but its audit entry could not be appended."""


class DriftReport(BaseModel):
    """Derived (capsule store) vs metered (ledger) usage, and their difference.

    ``drift_* = derived_* - metered_*``: positive means the store holds
    capsules/bytes the ledger never counted (pre-metering uploads, a crash
    between unpack and count); negative means the ledger counts more than the
    store holds (out-of-band deletion, delete→re-upload under-count).
    """

    model_config = ConfigDict(frozen=True)

    checked_at: str
    capsule_dir: str
    derived_capsules: int
    derived_bytes: int
    metered_capsules: int
    metered_bytes: int
    drift_capsules: int
    drift_bytes: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_sync(self) -> bool:
        """True when both drift dimensions are zero."""
        return self.drift_capsules == 0 and self.drift_bytes == 0


class ReconciliationResult(BaseModel):
    """Outcome of one reconciliation run."""

    model_config = ConfigDict(frozen=True)

    report: DriftReport
    applied: bool
    workspace: str
    ref: str | None = None
    rows_recorded: int = 0
    audited: bool = False


def metered_sums(
    db_path: Path | None = None, *, conn: sqlite3.Connection | None = None
) -> tuple[int, int]:
    """Lifetime metered ``(capsules_created, bytes_stored)`` over every workspace.

    Rollups + not-yet-finalized counters + the retention carry
    (:func:`usage.lifetime_totals`) — the same whole-history window the
    capsule-store scan covers, so rollup pruning never shows up as drift.
    """
    totals = usage.lifetime_totals(db_path=db_path, conn=conn)
    capsules = sum(t.get(usage.METRIC_CAPSULES, 0) for t in totals.values())
    size = sum(t.get(usage.METRIC_BYTES, 0) for t in totals.values())
    return int(capsules), int(size)


def measure_drift(
    capsule_dir: Path,
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
    conn: sqlite3.Connection | None = None,
    derived: QuotaUsage | None = None,
) -> DriftReport:
    """Measure derived vs metered usage and return the drift. Read-only.

    Pass *conn* to read the ledger inside a caller-owned transaction, and
    *derived* to reuse a store scan taken earlier (the apply path scans the
    store before taking the write lock).
    """
    now_dt = now or datetime.now(timezone.utc)
    store = derived if derived is not None else measure_capsule_store(capsule_dir)
    metered_capsules, metered_bytes = metered_sums(db_path, conn=conn)
    return DriftReport(
        checked_at=now_dt.isoformat(),
        capsule_dir=str(capsule_dir),
        derived_capsules=store.capsules,
        derived_bytes=store.total_bytes,
        metered_capsules=metered_capsules,
        metered_bytes=metered_bytes,
        drift_capsules=store.capsules - metered_capsules,
        drift_bytes=store.total_bytes - metered_bytes,
    )


def adjustment_entries(
    report: DriftReport, *, workspace: str, org: str, actor: str
) -> list[usage.LedgerEntry]:
    """The signed adjustment rows that close *report*'s drift (pure).

    One row per non-zero dimension; an in-sync report yields ``[]``.
    """
    ref = f"{RECON_REF_PREFIX}{report.checked_at}"
    pairs = (
        (usage.METRIC_CAPSULES, report.drift_capsules),
        (usage.METRIC_BYTES, report.drift_bytes),
    )
    return [
        usage.LedgerEntry(
            metric=metric,
            amount=amount,
            ref=ref,
            workspace=workspace,
            org=org,
            attribution=ATTRIBUTION_RECONCILIATION,
            actor=actor,
        )
        for metric, amount in pairs
        if amount != 0
    ]


def _refuse_negative_default(report: DriftReport, workspace: str, conn: sqlite3.Connection) -> None:
    """Refuse an adjustment that would drive *workspace* below zero."""
    current = usage.lifetime_totals(workspace=workspace, conn=conn).get(workspace, {})
    after_caps = int(current.get(usage.METRIC_CAPSULES, 0)) + report.drift_capsules
    after_bytes = int(current.get(usage.METRIC_BYTES, 0)) + report.drift_bytes
    if after_caps < 0 or after_bytes < 0:
        raise ReconciliationRefusedError(
            f"refusing adjustment: it would drive workspace {workspace!r} below "
            f"zero (capsules={after_caps}, bytes={after_bytes}). The ledger "
            "over-counts usage that belongs to another workspace; attributing "
            "it would be a guess (ADR-0208). Investigate out-of-band deletions."
        )


def _audit_run(result: ReconciliationResult, actor: str, audit_log_path: Path | None) -> None:
    """Append one ``usage.reconcile`` entry to the hash-chained audit log."""
    from novafabric.audit import AuditEventType, AuditLog, _paths

    details: dict[str, Any] = {
        "applied": result.applied,
        "workspace": result.workspace,
        "ref": result.ref,
        "rows_recorded": result.rows_recorded,
        **result.report.model_dump(),
    }
    AuditLog(audit_log_path or _paths.resolve_audit_log_path()).append(
        event_type=AuditEventType.USAGE_RECONCILE,
        actor=actor,
        resource_id=result.ref or f"report:{result.report.checked_at}",
        details=details,
    )


def reconcile(
    capsule_dir: Path,
    *,
    apply: bool = False,
    actor: str = "cli",
    db_path: Path | None = None,
    audit_log_path: Path | None = None,
    now: datetime | None = None,
    rollup_retention_months: int = 24,
    ledger_retention_months: int = 3,
) -> ReconciliationResult:
    """Report the ledger↔store drift; with *apply*, append the adjustment rows.

    Raises:
        ReconciliationRefusedError: *apply* would drive the default workspace
            below zero (nothing is written).
        ReconciliationAuditError: the adjustment committed (see ``ref``) but
            the audit entry failed — surfaced loudly, never swallowed.

    A report-only run whose audit append fails still returns its report, with
    ``audited=False`` and a logged warning (a read must not fail on the audit
    sink; a mutation must).
    """
    from novafabric.server.workspace_store import DEFAULT_WORKSPACE_SLUG

    workspace = DEFAULT_WORKSPACE_SLUG

    if not apply:
        report = measure_drift(capsule_dir, db_path=db_path, now=now)
        return _audit_report_only(report, workspace, actor, audit_log_path)

    org = usage.org_for_workspace(workspace, db_path)
    # The store scan (os.walk) runs BEFORE the lock so a large store never
    # holds metering writers (uploads) behind it.
    derived = measure_capsule_store(capsule_dir)
    conn = usage.open_usage_db(db_path)
    try:
        # Take the write lock BEFORE reading the ledger side: a concurrent
        # --apply blocks here and re-reads the ledger after our commit, so
        # the same drift is never booked twice (ADR-0208 P2 "Serialized
        # apply").
        conn.execute("BEGIN IMMEDIATE")
        try:
            report = measure_drift(
                capsule_dir, db_path=db_path, now=now, conn=conn, derived=derived
            )
            if report.in_sync:
                conn.rollback()
                return _audit_report_only(report, workspace, actor, audit_log_path)
            _refuse_negative_default(report, workspace, conn)
            entries = adjustment_entries(report, workspace=workspace, org=org, actor=actor)
            recorded = usage.record_entries_in_transaction(
                conn,
                entries,
                now=now,
                rollup_retention_months=rollup_retention_months,
                ledger_retention_months=ledger_retention_months,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()

    result = ReconciliationResult(
        report=report,
        applied=recorded > 0,
        workspace=workspace,
        ref=entries[0].ref,
        rows_recorded=recorded,
    )
    try:
        _audit_run(result, actor, audit_log_path)
    except Exception as exc:
        raise ReconciliationAuditError(
            f"adjustment {result.ref} recorded ({recorded} row(s)) but the audit "
            f"entry could not be appended: {exc}"
        ) from exc
    return result.model_copy(update={"audited": True})


def _audit_report_only(
    report: DriftReport, workspace: str, actor: str, audit_log_path: Path | None
) -> ReconciliationResult:
    """Audit a run that wrote nothing; an audit failure is logged, not raised."""
    result = ReconciliationResult(report=report, applied=False, workspace=workspace)
    try:
        _audit_run(result, actor, audit_log_path)
    except Exception:  # noqa: BLE001 — a read never fails on the audit sink
        logger.warning("usage reconcile: audit append failed", exc_info=True)
        return result
    return result.model_copy(update={"audited": True})
