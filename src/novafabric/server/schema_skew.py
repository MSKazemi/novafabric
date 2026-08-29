"""Schema-skew comparator and fail-closed startup guard (ADR-0211, experimental).

One revision comparator — :func:`compare_revisions` — shared by the server
lifespan guard, ``/readyz``'s migrations check, and
``nova doctor --check-storage``. It compares the database's
``alembic_version`` stamp against the packaged registry-track script head and
derives one of five honest states (spec ``pg-restore-skew-guard-v0.md``):

========================  =====================================================
``ok``                    stamp equals the script head
``behind``                stamp is an ancestor of head — new code, old schema
``ahead_or_foreign``      stamp unknown to this build — old code, newer schema
``unstamped``             no ``alembic_version`` table (or empty) — every
                          ``init_schema()``-bootstrapped deployment today
``unknown``               head unresolvable or DB unreadable — never faked
========================  =====================================================

:func:`enforce_schema_skew_guard` applies the normative decision table in the
server lifespan **before** ``init_schema()``: ``behind`` and
``ahead_or_foreign`` refuse to start (:class:`SchemaSkewError`, message
contracts E-SKEW-BEHIND / E-SKEW-AHEAD); ``NOVAFABRIC_ALLOW_SCHEMA_SKEW=1``
downgrades both to one structured warning; ``unstamped`` and ``unknown`` warn
and start --- except that an ``unstamped`` Postgres target with **no
application tables at all** is refused (E-SKEW-EMPTY), because a server whose
metadata plane cannot work would otherwise answer uploads with 201 and write
nothing. Messages carry backend name and revisions only — never DSNs,
hostnames, or credentials.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Optional, Union

from novafabric.migrations import registry_track

logger = logging.getLogger(__name__)

#: Escape hatch (spec: any of 1|true|yes, case-insensitive; all else is off).
ALLOW_SCHEMA_SKEW_ENV = "NOVAFABRIC_ALLOW_SCHEMA_SKEW"
_ALLOW_VALUES = frozenset({"1", "true", "yes"})

STATUS_OK: Final = "ok"
STATUS_BEHIND: Final = "behind"
STATUS_AHEAD: Final = "ahead_or_foreign"
STATUS_UNSTAMPED: Final = "unstamped"
STATUS_UNKNOWN: Final = "unknown"

SkewStatus = Literal["ok", "behind", "ahead_or_foreign", "unstamped", "unknown"]

#: Stamp read outcomes (internal).
_STAMPED = "stamped"
_UNSTAMPED = "unstamped"
_UNREADABLE = "unreadable"


class SchemaSkewError(RuntimeError):
    """Startup refused: the database schema does not match this build (ADR-0211 D3)."""

    def __init__(self, message: str, *, report: "SkewReport") -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class SkewReport:
    """Outcome of one revision comparison. Carries revisions, never targets."""

    status: SkewStatus
    backend: str
    track: str
    db_revision: Optional[str]
    head_revision: Optional[str]
    detail: str

    @property
    def remediation(self) -> str:
        """The command (or action) that fixes this state, for messages/logs."""
        if self.status == STATUS_BEHIND:
            return f"nova db upgrade --track registry --backend {self.backend}"
        if self.status == STATUS_AHEAD:
            return "upgrade the novafabric package to match the database"
        return "nova doctor --check-storage"

    def log_fields(self, *, event: str) -> dict[str, object]:
        """Structured fields for the guard's single log record / exception."""
        return {
            "event": event,
            "status": self.status,
            "backend": self.backend,
            "db_revision": self.db_revision,
            "head_revision": self.head_revision,
            "remediation": self.remediation,
        }


def allow_schema_skew(environ: Optional[dict[str, str]] = None) -> bool:
    """True when the operator set the documented break-glass env var."""
    env = environ if environ is not None else os.environ
    return env.get(ALLOW_SCHEMA_SKEW_ENV, "").strip().lower() in _ALLOW_VALUES


# ---------------------------------------------------------------------------
# Stamp readers (DSN/path never logged; bounded timeouts)
# ---------------------------------------------------------------------------


def _read_stamp_sqlite(db_path: Union[str, Path]) -> tuple[Optional[str], str]:
    """``(revision, state)`` from a SQLite file. Missing file ⇒ unstamped.

    A database file that does not exist yet is a fresh install about to be
    bootstrapped — the same operational state as an ``init_schema()``-created
    DB with no ``alembic_version`` table, so it maps to *unstamped* (start with
    a warning), not *unknown*.
    """
    path = Path(db_path)
    if not path.exists():
        return None, _UNSTAMPED
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None, _UNSTAMPED
        return None, _UNREADABLE
    except sqlite3.Error:
        return None, _UNREADABLE
    if row is None or not row[0]:
        return None, _UNSTAMPED
    return str(row[0]), _STAMPED


def _read_stamp_postgres(dsn: str) -> tuple[Optional[str], str]:
    """``(revision, state)`` from Postgres. The DSN is never logged or echoed."""
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        return None, _UNREADABLE
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            try:
                row = conn.execute(
                    "SELECT version_num FROM alembic_version LIMIT 1"
                ).fetchone()
            except psycopg.errors.UndefinedTable:
                return None, _UNSTAMPED
    except Exception:  # noqa: BLE001 — unreachable / auth failure / timeout
        return None, _UNREADABLE
    if row is None or not row[0]:
        return None, _UNSTAMPED
    return str(row[0]), _STAMPED


def postgres_application_tables(dsn: str) -> Optional[int]:
    """Count application tables in the target's ``public`` schema, or None.

    ``alembic_version`` is excluded: it is bookkeeping, not schema. None means
    "could not tell" (psycopg absent, unreachable, auth failure) and must never
    be treated as zero — the honesty rule forbids refusing on ignorance.

    Used to split the ``unstamped`` state in two. An ``init_schema()``-created
    deployment is unstamped *and populated*; a database whose migration never
    ran is unstamped *and empty*, and only the second one cannot work.
    """
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with psycopg.connect(dsn, connect_timeout=2) as conn:
            row = conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
            ).fetchone()
    except Exception:  # noqa: BLE001 — unreachable / auth failure / timeout
        return None
    if row is None or row[0] is None:
        return None
    return int(row[0])


def empty_backend_message(backend: str) -> str:
    """E-SKEW-EMPTY: the configured backend has no schema at all."""
    return (
        f"Refusing to start: the configured {backend} metadata backend contains "
        "no tables — its migration has not run. The capsule store would still "
        "accept uploads and return 201 while writing no metadata at all, so the "
        "server refuses rather than report success it cannot deliver. "
        "Run `nova db upgrade --backend postgres` (see `nova doctor "
        "--check-storage`). "
        f"Override: {ALLOW_SCHEMA_SKEW_ENV}=1."
    )


# ---------------------------------------------------------------------------
# Comparator (ADR-0211 D1)
# ---------------------------------------------------------------------------


def compare_revisions(
    backend: str,
    target: Union[str, Path, None],
    track: str = "registry",
    *,
    script_dir: Optional[Path] = None,
) -> SkewReport:
    """Compare *target*'s ``alembic_version`` stamp against the script head.

    Args:
        backend: ``"sqlite"`` (target = DB file path) or ``"postgres"``
            (target = DSN — treated as a secret, never logged).
        track: only ``"registry"`` is implemented in P1 (the MetadataStore
            track is deferred per the spec).
        script_dir: explicit script tree (tests); defaults to the packaged
            tree with source-checkout fallback (ADR-0211 D2).
    """
    if track != "registry":
        return SkewReport(
            status=STATUS_UNKNOWN,
            backend=backend,
            track=track,
            db_revision=None,
            head_revision=None,
            detail=f"track {track!r} is not implemented in P1 (registry only)",
        )
    if backend not in ("sqlite", "postgres"):
        return SkewReport(
            status=STATUS_UNKNOWN,
            backend=backend,
            track=track,
            db_revision=None,
            head_revision=None,
            detail=f"unknown backend {backend!r}",
        )
    if target is None or (isinstance(target, str) and not target):
        return SkewReport(
            status=STATUS_UNKNOWN,
            backend=backend,
            track=track,
            db_revision=None,
            head_revision=None,
            detail="no database target configured",
        )

    head = registry_track.script_head(backend, script_dir=script_dir)

    if backend == "sqlite":
        stamp, state = _read_stamp_sqlite(target)
    else:
        stamp, state = _read_stamp_postgres(str(target))

    if state == _UNREADABLE:
        return SkewReport(
            status=STATUS_UNKNOWN,
            backend=backend,
            track=track,
            db_revision=None,
            head_revision=head,
            detail="database unreadable — revision not determinable",
        )
    if state == _UNSTAMPED:
        return SkewReport(
            status=STATUS_UNSTAMPED,
            backend=backend,
            track=track,
            db_revision=None,
            head_revision=head,
            detail="no alembic_version stamp (init_schema-bootstrapped or fresh)",
        )
    if head is None:
        return SkewReport(
            status=STATUS_UNKNOWN,
            backend=backend,
            track=track,
            db_revision=stamp,
            head_revision=None,
            detail="migration script head unresolvable (lean install?)",
        )
    if stamp == head:
        return SkewReport(
            status=STATUS_OK,
            backend=backend,
            track=track,
            db_revision=stamp,
            head_revision=head,
            detail="database stamp matches the script head",
        )
    ancestry = registry_track.head_ancestry(backend, script_dir=script_dir)
    if ancestry is not None and stamp in ancestry:
        return SkewReport(
            status=STATUS_BEHIND,
            backend=backend,
            track=track,
            db_revision=stamp,
            head_revision=head,
            detail="database stamp is an ancestor of the script head",
        )
    return SkewReport(
        status=STATUS_AHEAD,
        backend=backend,
        track=track,
        db_revision=stamp,
        head_revision=head,
        detail="database stamp is not known to this build",
    )


# ---------------------------------------------------------------------------
# Message contracts (spec §"Error / warning message contracts")
# ---------------------------------------------------------------------------


def behind_message(report: SkewReport) -> str:
    """E-SKEW-BEHIND (contract-tested; backend + revisions only, never targets)."""
    return (
        f"Database schema is behind this build: revision {report.db_revision}, "
        f"expected head {report.head_revision} "
        f"(backend={report.backend}, track={report.track}).\n"
        "Refusing to start: running new code on an old schema fails at query time.\n"
        "Fix: back up, then run "
        f"`nova db upgrade --track registry --backend {report.backend}`.\n"
        "Emergency override (read-mostly, unsupported): "
        f"{ALLOW_SCHEMA_SKEW_ENV}=1."
    )


def ahead_message(report: SkewReport) -> str:
    """E-SKEW-AHEAD (contract-tested)."""
    return (
        f"Database schema revision {report.db_revision} is not known to this "
        f"build (head: {report.head_revision}). The database was migrated by a "
        "newer or different NovaFabric.\n"
        "Refusing to start. Fix: upgrade the novafabric package to match the "
        "database; do NOT downgrade the schema. "
        f"Override: {ALLOW_SCHEMA_SKEW_ENV}=1."
    )


# ---------------------------------------------------------------------------
# Startup guard (ADR-0211 D3)
# ---------------------------------------------------------------------------


def enforce_schema_skew_guard(
    *,
    backend: str,
    target: Union[str, Path, None],
    track: str = "registry",
    script_dir: Optional[Path] = None,
) -> SkewReport:
    """Apply the normative decision table at server startup.

    Runs in the lifespan **before** ``init_schema()`` and the org bootstrap —
    a refused server mutates nothing. Emits exactly one log record per
    startup. Raises :class:`SchemaSkewError` on ``behind`` /
    ``ahead_or_foreign`` unless ``NOVAFABRIC_ALLOW_SCHEMA_SKEW`` downgrades
    the refusal to a structured warning.
    """
    report = compare_revisions(backend, target, track, script_dir=script_dir)

    if report.status == STATUS_OK:
        logger.debug(
            "schema-skew guard: ok (backend=%s revision=%s)",
            report.backend,
            report.db_revision,
            extra=report.log_fields(event="schema_skew"),
        )
        return report

    if report.status in (STATUS_BEHIND, STATUS_AHEAD):
        message = (
            behind_message(report)
            if report.status == STATUS_BEHIND
            else ahead_message(report)
        )
        if allow_schema_skew():
            # W-SKEW-BEHIND / W-SKEW-AHEAD: byte-identical fields, overridden.
            logger.warning(
                "SCHEMA SKEW OVERRIDDEN (%s=1) — %s",
                ALLOW_SCHEMA_SKEW_ENV,
                message,
                extra=report.log_fields(event="schema_skew_overridden"),
            )
            return report
        raise SchemaSkewError(message, report=report)

    if report.status == STATUS_UNSTAMPED:
        # B14 (campaign 3): `unstamped` conflated two very different states.
        # An init_schema()-bootstrapped database is unstamped but *populated*
        # and works fine. A database whose migration never ran is unstamped and
        # *empty*, and nothing on the metadata plane can work — yet the server
        # started on a single warning and then answered 41,774 capsule uploads
        # with 201 while writing zero rows and logging zero errors. The capsules
        # were durable, so nothing was lost; what failed was the server's report
        # of what it had done. Refuse instead, for the configured server-mode
        # backend only — local SQLite mode is untouched, and an unreadable
        # target still starts (never refuse on ignorance).
        if backend == "postgres" and isinstance(target, str) and target:
            table_count = postgres_application_tables(target)
            if table_count == 0:
                message = empty_backend_message(backend)
                if allow_schema_skew():
                    logger.warning(
                        "EMPTY METADATA BACKEND OVERRIDDEN (%s=1) — %s",
                        ALLOW_SCHEMA_SKEW_ENV,
                        message,
                        extra=report.log_fields(event="schema_empty_overridden"),
                    )
                    return report
                raise SchemaSkewError(message, report=report)
        logger.warning(
            "schema-skew guard: database is not alembic-stamped (backend=%s) — "
            "starting; stamping adoption is planned. Inspect with "
            "`nova doctor --check-storage`.",
            report.backend,
            extra=report.log_fields(event="schema_skew"),
        )
        return report

    # STATUS_UNKNOWN — never refuse, never claim ok (ADR-0182 honesty rule).
    logger.warning(
        "schema-skew guard: revision state unknown (backend=%s: %s) — starting; "
        "inspect with `nova doctor --check-storage`.",
        report.backend,
        report.detail,
        extra=report.log_fields(event="schema_skew"),
    )
    return report
