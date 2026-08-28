"""Per-workspace usage metering (ADR-0208 P1, experimental).

Registry-SQLite ledger + counters + lazy monthly rollups, per
``the private design/spec/usage-metering-v0.md``:

- ``usage_ledger`` — append-only journal, one row per countable act, with a
  partial unique index on ``(metric, ref)`` so replayed inserts are no-ops
  (idempotent counting by construction);
- ``usage_counters`` — per ``(workspace, period, metric)`` running totals,
  updated in the **same SQLite transaction** as the ledger insert;
- ``usage_rollups`` — write-once monthly freezes of past-period counters,
  finalized lazily at the first metering write of a new period (no cron),
  retention-pruned opportunistically on write.

The ledger is the source of truth for **attribution**; the capsule store
remains the source of truth for **global** usage (``measure_capsule_store``
in ``quotas.py``). Drift between the two is a first-class, reported number
(``GET /v0/usage`` — ``server/routes/usage.py``).

House rules honored here:

- metering failures never fail the metered operation (callers wrap);
- ledger + counter are atomic with each other, NOT with the filesystem
  capsule write (no cross-substrate transaction exists — spec, stated
  honestly);
- ``api_requests`` is never a per-request DB write: the bounded
  :class:`ApiRequestAccumulator` flushes at most once per interval
  (the ADR-0193 D4 deliberate-coarseness precedent);
- corrections and deletions are new rows with negative ``amount`` — never
  UPDATE/DELETE of ledger rows (append-only, ``events/model.py`` discipline).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ulid import ULID

if TYPE_CHECKING:
    from novafabric.server.auth import AuthContext
    from novafabric.server.config import ServerConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

METRIC_CAPSULES = "capsules_created"
METRIC_BYTES = "bytes_stored"
METRIC_EVENTS = "events_ingested"
METRIC_API_REQUESTS = "api_requests"
METRIC_RECONCILIATION = "reconciliation"

#: The four P1 metrics every ``GET /v0/usage`` workspace row reports.
REPORTED_METRICS = (
    METRIC_CAPSULES,
    METRIC_BYTES,
    METRIC_EVENTS,
    METRIC_API_REQUESTS,
)

ATTRIBUTION_KEY = "key"
ATTRIBUTION_MEMBERSHIP = "membership"
ATTRIBUTION_DEFAULT = "default"

#: Default TTL of the enforcement-side usage cache (spec: same 5 s default
#: as ``quotas.DEFAULT_CACHE_TTL_SECONDS``).
DEFAULT_USAGE_CACHE_TTL_SECONDS = 5.0

# Schemas normative in the private design/spec/usage-metering-v0.md (three-table DDL).
_DDL = """
CREATE TABLE IF NOT EXISTS usage_ledger (
    ledger_id    TEXT PRIMARY KEY,
    org          TEXT NOT NULL,
    workspace    TEXT NOT NULL,
    metric       TEXT NOT NULL CHECK (metric IN
                   ('capsules_created','bytes_stored',
                    'events_ingested','api_requests','reconciliation')),
    amount       INTEGER NOT NULL,
    ref          TEXT,
    period       TEXT NOT NULL,
    attribution  TEXT NOT NULL CHECK (attribution IN
                   ('key','membership','default','reconciliation')),
    actor        TEXT NOT NULL,
    recorded_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ul_metric_ref
    ON usage_ledger(metric, ref) WHERE ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_ul_ws_period ON usage_ledger(workspace, period);

CREATE TABLE IF NOT EXISTS usage_counters (
    workspace    TEXT NOT NULL,
    period       TEXT NOT NULL,
    metric       TEXT NOT NULL,
    total        INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (workspace, period, metric)
);

CREATE TABLE IF NOT EXISTS usage_rollups (
    org          TEXT NOT NULL,
    workspace    TEXT NOT NULL,
    period       TEXT NOT NULL,
    metric       TEXT NOT NULL,
    total        INTEGER NOT NULL,
    finalized_at TEXT NOT NULL,
    PRIMARY KEY (workspace, period, metric)
);
"""


def metering_active(config: ServerConfig) -> bool:
    """True when accounting runs: master switch AND the metering kill-switch.

    Behind the existing ADR-0179 experimental flag (``server.rate_limits
    .enabled``); ``server.usage.metering_enabled`` is the accounting
    kill-switch that keeps rate limits on while metering is off.
    """
    return bool(config.rate_limits.enabled and config.usage.metering_enabled)


# ---------------------------------------------------------------------------
# Connection / period helpers
# ---------------------------------------------------------------------------


# Idle "anchor" connections, one per registry DB path (bounded LRU). While an
# anchor is open, the per-call connections below are never the *last* WAL
# connection, so closing them skips SQLite's checkpoint-on-last-close fsync —
# measured on the ingest hot path: p95 ~25 ms → ~0.3 ms per metering
# transaction (ADR-0208 acceptance: p95 < 5 ms local SQLite). WAL growth
# stays bounded by SQLite's ordinary commit-time auto-checkpoint. Anchors
# hold no transactions and no data — dropping them (LRU eviction,
# :func:`close_anchors`, process exit) is always safe.
_MAX_ANCHORS = 8
_anchors: OrderedDict[str, sqlite3.Connection] = OrderedDict()
_anchors_lock = threading.Lock()


def _ensure_anchor(resolved: Path) -> None:
    key = str(resolved)
    with _anchors_lock:
        if key in _anchors:
            _anchors.move_to_end(key)
            return
        try:
            anchor = sqlite3.connect(key, check_same_thread=False)
            # Touch the database so the anchor actually opens the WAL file —
            # an idle handle that never read anything does not keep the WAL
            # open, and the checkpoint-on-last-close would still fire.
            anchor.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            _anchors[key] = anchor
        except sqlite3.Error:  # pragma: no cover — anchor is an optimization
            return
        while len(_anchors) > _MAX_ANCHORS:
            _, old = _anchors.popitem(last=False)
            try:
                old.close()
            except sqlite3.Error:  # pragma: no cover
                pass


def close_anchors() -> None:
    """Close all cached anchor connections (tests / graceful shutdown)."""
    with _anchors_lock:
        for conn in _anchors.values():
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
        _anchors.clear()


def _get_conn(db_path: Path | None) -> sqlite3.Connection:
    from novafabric.registry.store import get_connection, get_db_path

    resolved = db_path or get_db_path()
    conn = get_connection(resolved)
    conn.row_factory = sqlite3.Row
    # WAL + synchronous=NORMAL for metering commits (connection-local; other
    # registry writers keep their own durability settings). Worst case under
    # power loss is losing the last committed metering transaction — exactly
    # the bounded ≤1-capsule undercount the spec already accepts and the
    # drift block surfaces.
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_DDL)
    conn.commit()
    _ensure_anchor(resolved)
    return conn


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def period_for(now: datetime | None = None) -> str:
    """UTC accounting period ``YYYY-MM`` for *now* (default: current time)."""
    now = now or _utcnow()
    return f"{now.year:04d}-{now.month:02d}"


def _months_before(period: str, months: int) -> str:
    """The period *months* calendar months before *period* (both ``YYYY-MM``)."""
    year, month = (int(p) for p in period.split("-"))
    index = year * 12 + (month - 1) - months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def dir_size_bytes(path: Path) -> int:
    """Total bytes of every file under *path* (unpacked capsule size)."""
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:  # pragma: no cover — file vanished mid-scan
            continue
    return total


# ---------------------------------------------------------------------------
# Attribution (spec: key binding -> single membership -> default workspace)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attribution:
    """Resolved workspace/org attribution for one countable act."""

    workspace: str
    org: str
    source: str  # 'key' | 'membership' | 'default'


def resolve_attribution(
    auth: AuthContext | None, db_path: Path | None
) -> Attribution:
    """Resolve the acting principal's workspace per the spec's normative order.

    1. API-key workspace binding (ADR-0193; stored-but-unenforced — metering
       consumes it as attribution regardless, inheriting that gap honestly);
    2. the principal's ADR-0178 workspace membership, when exactly one;
    3. the ADR-0178 default workspace (``attribution='default'``); ambiguous
       membership also resolves here — never a guess among candidates.

    ``org`` is denormalized from the workspace at record time. Any store
    error resolves to the default workspace (metering must never raise).
    """
    from novafabric.server.workspace_store import (
        DEFAULT_ORG_SLUG,
        DEFAULT_WORKSPACE_SLUG,
    )

    default = Attribution(
        workspace=DEFAULT_WORKSPACE_SLUG,
        org=DEFAULT_ORG_SLUG,
        source=ATTRIBUTION_DEFAULT,
    )
    try:
        conn = _get_conn(db_path)
    except Exception:  # noqa: BLE001 — attribution must never raise
        logger.warning("usage attribution store unavailable", exc_info=True)
        return default
    try:
        binding = getattr(auth, "workspace", None) if auth is not None else None
        if binding:
            row = conn.execute(
                "SELECT w.slug AS ws, o.slug AS org FROM workspaces w"
                " JOIN organizations o ON o.id = w.org_id"
                " WHERE w.slug = ? ORDER BY o.slug LIMIT 1",
                (binding,),
            ).fetchone()
            # An unknown binding still attributes to the bound slug (visible,
            # not laundered); its org falls back to the default org.
            org = row["org"] if row is not None else DEFAULT_ORG_SLUG
            return Attribution(
                workspace=str(binding), org=org, source=ATTRIBUTION_KEY
            )
        if auth is not None and auth.subject:
            rows = conn.execute(
                "SELECT DISTINCT w.slug AS ws, o.slug AS org FROM memberships m"
                " JOIN workspaces w ON w.id = m.scope_id"
                " JOIN organizations o ON o.id = w.org_id"
                " WHERE m.principal = ? AND m.scope_type = 'workspace'",
                (auth.subject,),
            ).fetchall()
            if len(rows) == 1:
                return Attribution(
                    workspace=rows[0]["ws"],
                    org=rows[0]["org"],
                    source=ATTRIBUTION_MEMBERSHIP,
                )
        return default
    except sqlite3.OperationalError:
        # Workspace tables not bootstrapped yet (pre-ADR-0178 registry) —
        # expected in local/dev shapes; everything lands in the default
        # workspace, honestly labeled `attribution='default'`.
        logger.debug("usage attribution: workspace tables absent; using default")
        return default
    except Exception:  # noqa: BLE001 — attribution must never raise
        logger.warning("usage attribution resolution failed", exc_info=True)
        return default
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recording (ledger + counters, one transaction; lazy rollups + retention)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    """One countable act destined for the ledger."""

    metric: str
    amount: int
    ref: str | None
    workspace: str
    org: str
    attribution: str
    actor: str


def _finalize_and_prune(
    conn: sqlite3.Connection,
    current_period: str,
    *,
    rollup_retention_months: int,
    ledger_retention_months: int,
    now_iso: str,
) -> None:
    """Lazy rollup finalization + opportunistic retention pruning (spec D4).

    Runs inside the caller's transaction. Finalization copies every counter
    row of a period earlier than *current_period* into ``usage_rollups``
    (``INSERT OR IGNORE`` — write-once, re-finalization is a no-op). Pruning:
    rollups past ``rollup_retention_months``; raw ledger rows of finalized
    periods past ``ledger_retention_months``; counter rows of finalized
    periods past ledger retention (rollups carry the totals).
    """
    # Finalize: counters from past periods, org denormalized from the most
    # recent ledger row of that (workspace, period) — 'default' as last resort
    # for counter rows whose ledger rows were pruned first (defensive).
    conn.execute(
        """
        INSERT OR IGNORE INTO usage_rollups
            (org, workspace, period, metric, total, finalized_at)
        SELECT COALESCE(
                   (SELECT l.org FROM usage_ledger l
                     WHERE l.workspace = c.workspace AND l.period = c.period
                     ORDER BY l.recorded_at DESC LIMIT 1),
                   'default'),
               c.workspace, c.period, c.metric, c.total, ?
          FROM usage_counters c
         WHERE c.period < ?
        """,
        (now_iso, current_period),
    )
    rollup_cutoff = _months_before(current_period, rollup_retention_months)
    ledger_cutoff = _months_before(current_period, ledger_retention_months)
    # Ledger/counter pruning consults the rollups (only *finalized* periods
    # are prunable), so the rollup prune must run LAST in this transaction.
    conn.execute(
        "DELETE FROM usage_ledger WHERE period < ? AND period IN"
        " (SELECT DISTINCT period FROM usage_rollups)",
        (ledger_cutoff,),
    )
    conn.execute(
        "DELETE FROM usage_counters WHERE period < ? AND EXISTS"
        " (SELECT 1 FROM usage_rollups r WHERE r.workspace = usage_counters.workspace"
        "   AND r.period = usage_counters.period AND r.metric = usage_counters.metric)",
        (ledger_cutoff,),
    )
    conn.execute("DELETE FROM usage_rollups WHERE period < ?", (rollup_cutoff,))


def record_entries(
    entries: list[LedgerEntry],
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
    rollup_retention_months: int = 24,
    ledger_retention_months: int = 3,
) -> int:
    """Append *entries* to the ledger and bump counters — one SQLite transaction.

    Idempotency: an entry whose ``(metric, ref)`` already exists inserts zero
    rows (``INSERT OR IGNORE`` against the partial unique index) and its
    counter update is skipped — a replayed increment is a no-op (spec,
    normative). Returns the number of entries actually recorded.

    The first write of a new period lazily finalizes the previous period's
    counters into ``usage_rollups`` and prunes retention (spec D4).
    """
    if not entries:
        return 0
    now_dt = now or _utcnow()
    now_iso = now_dt.isoformat()
    period = period_for(now_dt)
    conn = _get_conn(db_path)
    try:
        recorded = 0
        with conn:  # one transaction: rollups + ledger + counters
            _finalize_and_prune(
                conn,
                period,
                rollup_retention_months=rollup_retention_months,
                ledger_retention_months=ledger_retention_months,
                now_iso=now_iso,
            )
            for e in entries:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO usage_ledger"
                    " (ledger_id, org, workspace, metric, amount, ref, period,"
                    "  attribution, actor, recorded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(ULID()),
                        e.org,
                        e.workspace,
                        e.metric,
                        e.amount,
                        e.ref,
                        period,
                        e.attribution,
                        e.actor,
                        now_iso,
                    ),
                )
                if cur.rowcount == 0:
                    continue  # duplicate (metric, ref) — replay is a no-op
                conn.execute(
                    "INSERT INTO usage_counters (workspace, period, metric, total,"
                    " updated_at) VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(workspace, period, metric)"
                    " DO UPDATE SET total = total + excluded.total, updated_at ="
                    " excluded.updated_at",
                    (e.workspace, period, e.metric, e.amount, now_iso),
                )
                recorded += 1
        return recorded
    finally:
        conn.close()


def record_capsule_upload(
    *,
    run_id: str,
    size_bytes: int,
    attribution: Attribution,
    actor: str,
    db_path: Path | None = None,
    now: datetime | None = None,
    rollup_retention_months: int = 24,
    ledger_retention_months: int = 3,
) -> int:
    """Meter one successful capsule upload: count + unpacked bytes (spec P1).

    ``ref = run_id`` for both metrics — the second idempotency guard behind
    the route's 409-on-duplicate. Ledger + counters commit atomically; the
    caller invokes this only **after** the unpack/publish succeeded.
    """
    entries = [
        LedgerEntry(
            metric=METRIC_CAPSULES,
            amount=1,
            ref=run_id,
            workspace=attribution.workspace,
            org=attribution.org,
            attribution=attribution.source,
            actor=actor,
        ),
        LedgerEntry(
            metric=METRIC_BYTES,
            amount=size_bytes,
            ref=run_id,
            workspace=attribution.workspace,
            org=attribution.org,
            attribution=attribution.source,
            actor=actor,
        ),
    ]
    return record_entries(
        entries,
        db_path=db_path,
        now=now,
        rollup_retention_months=rollup_retention_months,
        ledger_retention_months=ledger_retention_months,
    )


def record_capsule_delete(
    *,
    run_id: str,
    actor: str,
    db_path: Path | None = None,
    now: datetime | None = None,
    rollup_retention_months: int = 24,
    ledger_retention_months: int = 3,
) -> str | None:
    """Append negative adjustment rows for a deleted capsule (ADR-0206 compose).

    Mirrors the capsule's original metered rows (workspace, org, attribution,
    amount) with negated amounts and ``ref = "<run_id>:delete"`` — the ledger
    stays append-only and the adjustment is itself idempotent under the
    ``(metric, ref)`` unique index. A capsule with **no** metered upload rows
    (pre-metering, or already reversed) records nothing: attributing
    unmetered bytes to a workspace would be guesswork, which the spec refuses
    — such deletes surface only through the ``drift`` block.

    Returns the adjusted workspace slug, or None when nothing was recorded.
    """
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT metric, amount, workspace, org, attribution FROM usage_ledger"
            " WHERE ref = ? AND metric IN (?, ?) AND amount > 0",
            (run_id, METRIC_CAPSULES, METRIC_BYTES),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    entries = [
        LedgerEntry(
            metric=r["metric"],
            amount=-int(r["amount"]),
            ref=f"{run_id}:delete",
            workspace=r["workspace"],
            org=r["org"],
            attribution=r["attribution"],
            actor=actor,
        )
        for r in rows
    ]
    record_entries(
        entries,
        db_path=db_path,
        now=now,
        rollup_retention_months=rollup_retention_months,
        ledger_retention_months=ledger_retention_months,
    )
    return str(rows[0]["workspace"])


# ---------------------------------------------------------------------------
# Reading (reporting + enforcement)
# ---------------------------------------------------------------------------


def usage_for_period(
    period: str, *, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """Per-workspace metric totals for *period*, org attached.

    Past periods serve from ``usage_rollups``; periods not (yet) finalized —
    the current period, always — serve from ``usage_counters`` (org
    denormalized from the ledger). A period with no rows returns an empty
    list, never an error (spec).
    """
    conn = _get_conn(db_path)
    try:
        merged: dict[str, dict[str, Any]] = {}
        for row in conn.execute(
            "SELECT org, workspace, metric, total FROM usage_rollups"
            " WHERE period = ?",
            (period,),
        ):
            ws = merged.setdefault(
                row["workspace"],
                {
                    "org": row["org"],
                    "workspace": row["workspace"],
                    "metrics": dict.fromkeys(REPORTED_METRICS, 0),
                },
            )
            if row["metric"] in ws["metrics"]:
                ws["metrics"][row["metric"]] = int(row["total"])
        for row in conn.execute(
            """
            SELECT c.workspace, c.metric, c.total,
                   COALESCE((SELECT l.org FROM usage_ledger l
                              WHERE l.workspace = c.workspace AND l.period = c.period
                              ORDER BY l.recorded_at DESC LIMIT 1),
                            'default') AS org
              FROM usage_counters c
             WHERE c.period = ?
               AND NOT EXISTS (SELECT 1 FROM usage_rollups r
                                WHERE r.workspace = c.workspace
                                  AND r.period = c.period AND r.metric = c.metric)
            """,
            (period,),
        ):
            ws = merged.setdefault(
                row["workspace"],
                {
                    "org": row["org"],
                    "workspace": row["workspace"],
                    "metrics": dict.fromkeys(REPORTED_METRICS, 0),
                },
            )
            if row["metric"] in ws["metrics"]:
                ws["metrics"][row["metric"]] = int(row["total"])
        return sorted(merged.values(), key=lambda w: (w["org"], w["workspace"]))
    finally:
        conn.close()


def all_time_totals(
    *, db_path: Path | None = None, workspace: str | None = None
) -> dict[str, dict[str, int]]:
    """All-time metered sums per workspace: rollups + not-yet-finalized counters.

    This is the **enforcement** figure (negative delete adjustments included).
    Finalized periods count once (rollup preferred over any still-retained
    counter row). Honest bound: periods pruned past rollup retention no
    longer contribute — the figure is effectively a rolling
    ``rollup_retention_months`` window (spec D4).
    """
    conn = _get_conn(db_path)
    try:
        totals: dict[str, dict[str, int]] = {}
        clause = " AND workspace = ?" if workspace is not None else ""
        params: tuple[str, ...] = (workspace,) if workspace is not None else ()
        for row in conn.execute(
            f"SELECT workspace, metric, SUM(total) AS t FROM usage_rollups"
            f" WHERE 1=1{clause} GROUP BY workspace, metric",  # noqa: S608
            params,
        ):
            totals.setdefault(row["workspace"], {})[row["metric"]] = int(row["t"] or 0)
        for row in conn.execute(
            f"""
            SELECT c.workspace AS workspace, c.metric AS metric, SUM(c.total) AS t
              FROM usage_counters c
             WHERE NOT EXISTS (SELECT 1 FROM usage_rollups r
                                WHERE r.workspace = c.workspace
                                  AND r.period = c.period AND r.metric = c.metric)
                   {clause.replace("workspace", "c.workspace")}
             GROUP BY c.workspace, c.metric
            """,  # noqa: S608
            params,
        ):
            ws = totals.setdefault(row["workspace"], {})
            ws[row["metric"]] = ws.get(row["metric"], 0) + int(row["t"] or 0)
        return totals
    finally:
        conn.close()


class WorkspaceUsageReader:
    """TTL-cached all-time (capsules, bytes) reader for quota enforcement.

    Same bounded-staleness trade as the global checker's store cache
    (:data:`DEFAULT_USAGE_CACHE_TTL_SECONDS` = the quotas 5 s default).
    Thread-safe; the clock is injectable and MUST be monotonic.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        cache_ttl: float = DEFAULT_USAGE_CACHE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db_path = db_path
        self.cache_ttl = float(cache_ttl)
        self._clock = clock
        self._cache: dict[str, tuple[float, tuple[int, int]]] = {}
        self._lock = threading.Lock()

    def invalidate(self, workspace: str | None = None) -> None:
        """Drop the cached figure for *workspace* (or all when None)."""
        with self._lock:
            if workspace is None:
                self._cache.clear()
            else:
                self._cache.pop(workspace, None)

    def get(self, workspace: str) -> tuple[int, int]:
        """All-time metered ``(capsules_created, bytes_stored)`` for *workspace*."""
        now = self._clock()
        with self._lock:
            hit = self._cache.get(workspace)
            if hit is not None and (now - hit[0]) < self.cache_ttl:
                return hit[1]
        totals = all_time_totals(db_path=self._db_path, workspace=workspace).get(
            workspace, {}
        )
        value = (
            int(totals.get(METRIC_CAPSULES, 0)),
            int(totals.get(METRIC_BYTES, 0)),
        )
        with self._lock:
            self._cache[workspace] = (now, value)
        return value


# ---------------------------------------------------------------------------
# api_requests accumulator (hot-path bound — spec: never a per-request write)
# ---------------------------------------------------------------------------


class ApiRequestAccumulator:
    """Bounded in-process ``{workspace: count}`` map with interval flushing.

    LRU-bounded (default 10 000 entries — the ADR-0179 bucket-map
    discipline): above the bound the least-recently-bumped workspace's
    pending count is dropped with a log line. Counts lost in a crash are
    bounded by one flush interval. **Exactness is deliberately coarse —
    documented, not discovered** (spec).
    """

    def __init__(
        self,
        *,
        max_entries: int = 10_000,
        flush_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        rollup_retention_months: int = 24,
        ledger_retention_months: int = 3,
    ) -> None:
        self.max_entries = int(max_entries)
        self.flush_interval_s = float(flush_interval_s)
        self._clock = clock
        self._rollup_retention_months = int(rollup_retention_months)
        self._ledger_retention_months = int(ledger_retention_months)
        self._counts: OrderedDict[str, int] = OrderedDict()
        self._last_flush = clock()
        self._lock = threading.Lock()

    def add(self, workspace: str, n: int = 1) -> None:
        """Bump *workspace* by *n*; O(1), never touches the database."""
        with self._lock:
            if workspace in self._counts:
                self._counts[workspace] += n
                self._counts.move_to_end(workspace)
            else:
                self._counts[workspace] = n
                while len(self._counts) > self.max_entries:
                    evicted, lost = self._counts.popitem(last=False)
                    logger.debug(
                        "usage accumulator LRU-evicted %s (%d pending)",
                        evicted,
                        lost,
                    )

    def due(self) -> bool:
        """True when at least one flush interval elapsed since the last flush."""
        return (self._clock() - self._last_flush) >= self.flush_interval_s

    def flush(
        self,
        db_path: Path | None = None,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> int:
        """Write one ``api_requests`` ledger row per pending workspace.

        A no-op unless due (or *force*). Rows carry ``ref = NULL`` (no natural
        idempotency key — spec) and ``actor = 'system'``. Returns the number
        of rows written. Never raises (audit-on-error house rule is the
        caller's; here a failure restores nothing — bounded loss).
        """
        with self._lock:
            if not force and not self.due():
                return 0
            pending = dict(self._counts)
            self._counts.clear()
            self._last_flush = self._clock()
        if not pending:
            return 0
        entries = [
            LedgerEntry(
                metric=METRIC_API_REQUESTS,
                amount=count,
                ref=None,
                workspace=ws,
                org=_org_for_workspace(ws, db_path),
                # Aggregated across principals — per-principal attribution is
                # deliberately not preserved for api_requests (spec: coarse).
                attribution=ATTRIBUTION_DEFAULT,
                actor="system",
            )
            for ws, count in pending.items()
            if count
        ]
        return record_entries(
            entries,
            db_path=db_path,
            now=now,
            rollup_retention_months=self._rollup_retention_months,
            ledger_retention_months=self._ledger_retention_months,
        )


def _org_for_workspace(slug: str, db_path: Path | None) -> str:
    """Org slug for a workspace slug (first match); 'default' when unknown."""
    try:
        conn = _get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT o.slug AS org FROM workspaces w"
                " JOIN organizations o ON o.id = w.org_id"
                " WHERE w.slug = ? ORDER BY o.slug LIMIT 1",
                (slug,),
            ).fetchone()
            return row["org"] if row is not None else "default"
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — metering must never raise
        return "default"
