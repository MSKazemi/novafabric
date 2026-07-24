# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Persisted GDPR-erasure request queue (ADR-0210, experimental).

An ``erasure_requests`` table in a dedicated ``$NOVAFABRIC_HOME/erasure.db``
(sibling of ``dek.db`` — deliberately NOT a table in ``registry.db``).  Each
request is persisted PENDING *before* execution, then executed synchronously
through the exact code path of ``nova pii erase``
(:meth:`novafabric.pii.dek.DEKStore.erase_subject`) and transitioned to one of
the terminal states ``COMPLETED | DEFERRED | FAILED`` with a durable,
hash-verifiable receipt (``receipt_sha256``).

Privacy (normative, ADR-0210 D1): the plaintext ``subject_id`` is stored in
the queue rows and receipts only — it is the input ``erase_subject`` requires.
Structured logs emitted by this module carry ``subject_sha256`` prefixes,
never the raw subject.  Error details are sanitized so machinery exception
messages cannot echo the raw subject either.

Fail-closed (normative, ADR-0210 D3): every exception path ends in ``FAILED``
with an error receipt.  A row may remain ``PENDING`` only across a process
crash between commit and execution; the next duplicate POST re-attaches to it
(ADR-0210 D4 — idempotency and crash recovery via one mechanism).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from novafabric.audit import AUDIT_LOG_PATH
from novafabric.capture._ulid import new_ulid
from novafabric.pii.dek import ErasureDeferredReceipt, open_dek_store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# States and constants
# ---------------------------------------------------------------------------

STATE_PENDING = "PENDING"
STATE_COMPLETED = "COMPLETED"
STATE_DEFERRED = "DEFERRED"
STATE_FAILED = "FAILED"

TERMINAL_STATES = frozenset({STATE_COMPLETED, STATE_DEFERRED, STATE_FAILED})

DEFAULT_REASON = "gdpr_art_17"

RETENTION_MONTHS_ENV = "NOVA_AI_ACT_RETENTION_MONTHS"
DEFAULT_RETENTION_MONTHS = 6

_ERROR_DETAIL_MAX_LEN = 500


class ErasureQueueError(Exception):
    """Raised for invalid erasure-queue operations (unknown request, bad state)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def subject_sha256(subject_id: str) -> str:
    """Hex SHA-256 of the raw subject id — the only form allowed in logs."""
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def canonical_receipt_json(receipt: dict[str, Any]) -> str:
    """Canonical (sorted-keys, compact) JSON serialisation of a receipt."""
    return json.dumps(receipt, sort_keys=True, separators=(",", ":"))


def receipt_sha256_of(receipt: dict[str, Any]) -> str:
    """Verification hash: SHA-256 over the canonical receipt JSON bytes."""
    return hashlib.sha256(canonical_receipt_json(receipt).encode("utf-8")).hexdigest()


def sanitize_error_detail(detail: str, subject_id: str) -> str:
    """Replace any occurrence of the raw subject id with its hash prefix.

    Defense in depth: DEK-store exception messages can echo the primary key
    (e.g. ``KeyError: "No DEK found for subject_id='user@example.com'"``).
    """
    if subject_id:
        detail = detail.replace(
            subject_id, "sha256:" + subject_sha256(subject_id)[:12]
        )
    return detail[:_ERROR_DETAIL_MAX_LEN]


def retention_months_from_env() -> int:
    """Art.17(3)(b) retention window from ``NOVA_AI_ACT_RETENTION_MONTHS``."""
    raw = os.environ.get(RETENTION_MONTHS_ENV, "").strip()
    if not raw:
        return DEFAULT_RETENTION_MONTHS
    try:
        months = int(raw)
    except ValueError:
        logger.warning(
            "invalid %s=%r — falling back to %d",
            RETENTION_MONTHS_ENV,
            raw,
            DEFAULT_RETENTION_MONTHS,
        )
        return DEFAULT_RETENTION_MONTHS
    return max(months, 0)


def erasure_audit_log_path() -> Path:
    """Path of the hash-chained audit log the ``erasure.request`` events ride on.

    Module-level indirection so tests can monkeypatch
    ``novafabric.pii.erasure_queue.AUDIT_LOG_PATH`` (same pattern as
    ``registry.service``).
    """
    return AUDIT_LOG_PATH


# ---------------------------------------------------------------------------
# Record model
# ---------------------------------------------------------------------------


class ErasureRequestRecord(BaseModel):
    """Frozen view of one ``erasure_requests`` row."""

    request_id: str = Field(..., description="ULID primary key")
    subject_id: str = Field(..., description="Plaintext data-subject id (rows/receipts only)")
    subject_sha256: str = Field(..., description="Hex SHA-256 of subject_id")
    capsule_ids: list[str] = Field(default_factory=list)
    reason: str = Field(default=DEFAULT_REASON)
    state: str = Field(..., description="PENDING | COMPLETED | DEFERRED | FAILED")
    requested_at: str = Field(..., description="ISO-8601 UTC")
    requested_by: str = Field(default="", description="serve token fingerprint / principal")
    executed_at: str | None = Field(default=None, description="ISO-8601 UTC; NULL while PENDING")
    receipt: dict[str, Any] | None = Field(
        default=None, description="Full receipt (success, deferred, or error receipt)"
    )
    receipt_sha256: str | None = Field(
        default=None, description="SHA-256 over the canonical receipt JSON bytes"
    )
    error_class: str | None = None
    error_detail: str | None = None

    model_config = {"frozen": True}

    def api_view(self) -> dict[str, Any]:
        """Hash-only row view for API responses (no top-level raw subject).

        The raw subject still appears *inside* success/deferred receipts —
        the spec allows it on the authenticated ``/status`` surface.
        """
        return {
            "request_id": self.request_id,
            "subject_sha256": self.subject_sha256,
            "state": self.state,
            "reason": self.reason,
            "requested_at": self.requested_at,
            "requested_by": self.requested_by,
            "executed_at": self.executed_at,
            "capsule_ids": list(self.capsule_ids),
            "receipt": self.receipt,
            "receipt_sha256": self.receipt_sha256,
            "error_class": self.error_class,
            "error_detail": self.error_detail,
        }


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS erasure_requests (
    request_id     TEXT PRIMARY KEY,
    subject_id     TEXT NOT NULL,
    subject_sha256 TEXT NOT NULL,
    capsule_ids    TEXT NOT NULL DEFAULT '[]',
    reason         TEXT NOT NULL DEFAULT 'gdpr_art_17',
    state          TEXT NOT NULL DEFAULT 'PENDING',
    requested_at   TEXT NOT NULL,
    requested_by   TEXT NOT NULL DEFAULT '',
    executed_at    TEXT,
    receipt_json   TEXT,
    receipt_sha256 TEXT,
    error_class    TEXT,
    error_detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_erasure_requests_subject_state
    ON erasure_requests (subject_id, state);
"""

_COLUMNS = (
    "request_id, subject_id, subject_sha256, capsule_ids, reason, state, "
    "requested_at, requested_by, executed_at, receipt_json, receipt_sha256, "
    "error_class, error_detail"
)


class ErasureQueue:
    """SQLite-backed persisted erasure-request queue (WAL, 0600 posture).

    Same concurrency posture as :class:`novafabric.pii.dek.DEKStore`:
    ``check_same_thread=False`` + a process-level lock; each mutation runs in
    an explicit transaction.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_DDL)
        self._conn.commit()
        try:
            os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:  # pragma: no cover - platform-dependent
            pass

    # -- writes ---------------------------------------------------------

    def create_request(
        self,
        *,
        subject_id: str,
        capsule_ids: list[str] | None = None,
        reason: str = DEFAULT_REASON,
        requested_by: str = "",
    ) -> ErasureRequestRecord:
        """Persist a new PENDING request (committed before any execution)."""
        request_id = new_ulid()
        requested_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO erasure_requests "
                "(request_id, subject_id, subject_sha256, capsule_ids, reason, "
                " state, requested_at, requested_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    subject_id,
                    subject_sha256(subject_id),
                    json.dumps(list(capsule_ids or [])),
                    reason,
                    STATE_PENDING,
                    requested_at,
                    requested_by,
                ),
            )
        record = self.get(request_id)
        assert record is not None  # row committed just above
        logger.info(
            "erasure_request_created",
            extra={
                "event": "erasure_request_created",
                "request_id": request_id,
                "subject_sha256_prefix": record.subject_sha256[:12],
                "state": STATE_PENDING,
            },
        )
        return record

    def finalize(
        self,
        request_id: str,
        *,
        state: str,
        receipt: dict[str, Any] | None,
        error_class: str | None = None,
        error_detail: str | None = None,
    ) -> ErasureRequestRecord:
        """Transition a PENDING request to a terminal state with its receipt."""
        if state not in TERMINAL_STATES:
            raise ErasureQueueError(f"not a terminal state: {state!r}")
        receipt_json = canonical_receipt_json(receipt) if receipt is not None else None
        receipt_hash = receipt_sha256_of(receipt) if receipt is not None else None
        executed_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE erasure_requests SET state = ?, executed_at = ?, "
                "receipt_json = ?, receipt_sha256 = ?, error_class = ?, error_detail = ? "
                "WHERE request_id = ?",
                (
                    state,
                    executed_at,
                    receipt_json,
                    receipt_hash,
                    error_class,
                    error_detail,
                    request_id,
                ),
            )
            if cur.rowcount != 1:
                raise ErasureQueueError(f"unknown request_id: {request_id!r}")
        record = self.get(request_id)
        assert record is not None
        return record

    # -- reads ----------------------------------------------------------

    def get(self, request_id: str) -> ErasureRequestRecord | None:
        cur = self._conn.execute(
            f"SELECT {_COLUMNS} FROM erasure_requests WHERE request_id = ?",  # noqa: S608
            (request_id,),
        )
        row = cur.fetchone()
        return self._to_record(row) if row is not None else None

    def find_pending(self, subject_id: str) -> ErasureRequestRecord | None:
        """Oldest PENDING request for *subject_id* (re-attach target, ADR-0210 D4)."""
        cur = self._conn.execute(
            f"SELECT {_COLUMNS} FROM erasure_requests "  # noqa: S608
            "WHERE subject_id = ? AND state = ? ORDER BY request_id ASC LIMIT 1",
            (subject_id, STATE_PENDING),
        )
        row = cur.fetchone()
        return self._to_record(row) if row is not None else None

    def latest_completed(
        self, subject_id: str, *, exclude_request_id: str | None = None
    ) -> ErasureRequestRecord | None:
        """Most recent COMPLETED request for *subject_id* (already-erased proof)."""
        cur = self._conn.execute(
            f"SELECT {_COLUMNS} FROM erasure_requests "  # noqa: S608
            "WHERE subject_id = ? AND state = ? AND request_id != ? "
            "ORDER BY request_id DESC LIMIT 1",
            (subject_id, STATE_COMPLETED, exclude_request_id or ""),
        )
        row = cur.fetchone()
        return self._to_record(row) if row is not None else None

    def list_requests(
        self, *, subject_id: str | None = None, limit: int = 200
    ) -> list[ErasureRequestRecord]:
        """All requests, newest first; optional exact-match subject filter."""
        limit = max(1, int(limit))
        if subject_id is None:
            cur = self._conn.execute(
                f"SELECT {_COLUMNS} FROM erasure_requests "  # noqa: S608
                "ORDER BY request_id DESC LIMIT ?",
                (limit,),
            )
        else:
            cur = self._conn.execute(
                f"SELECT {_COLUMNS} FROM erasure_requests WHERE subject_id = ? "  # noqa: S608
                "ORDER BY request_id DESC LIMIT ?",
                (subject_id, limit),
            )
        return [self._to_record(row) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()

    # -- internal -------------------------------------------------------

    @staticmethod
    def _to_record(row: tuple[Any, ...]) -> ErasureRequestRecord:
        receipt_raw = row[9]
        receipt: dict[str, Any] | None = None
        if receipt_raw:
            try:
                receipt = json.loads(receipt_raw)
            except json.JSONDecodeError:  # pragma: no cover - defensive
                receipt = {"kind": "unparseable_receipt"}
        return ErasureRequestRecord(
            request_id=row[0],
            subject_id=row[1],
            subject_sha256=row[2],
            capsule_ids=json.loads(row[3]) if row[3] else [],
            reason=row[4],
            state=row[5],
            requested_at=row[6],
            requested_by=row[7] or "",
            executed_at=row[8],
            receipt=receipt,
            receipt_sha256=row[10],
            error_class=row[11],
            error_detail=row[12],
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def erasure_db_path(home: Path | None = None) -> Path:
    """``$NOVAFABRIC_HOME/erasure.db`` (or *home*/erasure.db)."""
    if home is None:
        home = Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))
    return home / "erasure.db"


def open_erasure_queue(home: Path | None = None) -> ErasureQueue:
    """Open the erasure queue at ``$NOVAFABRIC_HOME/erasure.db`` (or *home*)."""
    return ErasureQueue(erasure_db_path(home))


# ---------------------------------------------------------------------------
# Synchronous execution (the one code path — CLI parity)
# ---------------------------------------------------------------------------


def _write_receipt_file(
    receipt_dir: Path, record: ErasureRequestRecord
) -> str | None:
    """Persist the receipt under the retention receipt directory (best-effort).

    Naming matches ``retention/actions.py::_write_receipt`` so ADR-0181
    restore-time crypto-shred replay can resolve subjects from the file.
    A file-write failure never un-does a completed erasure: it is logged
    (hash-only) and the row's ``receipt_json`` remains the durable record.
    """
    if record.receipt is None:  # pragma: no cover - defensive
        return None
    suffix = (
        "deferred.receipt.json" if record.state == STATE_DEFERRED else "receipt.json"
    )
    path = receipt_dir / f"{record.request_id}.{suffix}"
    try:
        receipt_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record.receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "erasure_receipt_file_write_failed",
            extra={
                "event": "erasure_receipt_file_write_failed",
                "request_id": record.request_id,
                "subject_sha256_prefix": record.subject_sha256[:12],
                "error": str(exc),
            },
        )
        return None
    return str(path)


def execute_request(
    queue: ErasureQueue,
    record: ErasureRequestRecord,
    *,
    home: Path,
    retention_months: int | None = None,
) -> ErasureRequestRecord:
    """Execute one PENDING request synchronously through ``DEKStore.erase_subject``.

    Identical machinery call to ``nova pii erase`` (ADR-0069).  Outcome
    mapping per ADR-0210 D3 (fail-closed — every exception ends in FAILED):

    - ``ErasureReceipt``           -> COMPLETED (receipt verbatim)
    - ``ErasureDeferredReceipt``   -> DEFERRED (receipt verbatim)
    - ``KeyError`` + prior COMPLETED row -> COMPLETED (``already_erased``)
    - ``KeyError``, no prior completion  -> FAILED (``subject_not_found``)
    - any other exception          -> FAILED (error receipt, sanitized detail)
    """
    if record.state != STATE_PENDING:
        return record  # terminal already — nothing to (re-)execute
    if retention_months is None:
        retention_months = retention_months_from_env()
    hash_prefix = record.subject_sha256[:12]
    receipt_dir = home / "evidence" / "erasure"

    try:
        dek_store = open_dek_store(home)
        try:
            result = dek_store.erase_subject(
                subject_id=record.subject_id,
                capsule_ids=list(record.capsule_ids),
                retention_months=retention_months,
            )
        finally:
            dek_store.close()
    except KeyError as exc:
        prior = queue.latest_completed(
            record.subject_id, exclude_request_id=record.request_id
        )
        if prior is not None:
            receipt = {
                "kind": "already_erased",
                "prior_request_id": prior.request_id,
                "prior_receipt_sha256": prior.receipt_sha256,
            }
            final = queue.finalize(
                record.request_id, state=STATE_COMPLETED, receipt=receipt
            )
            _write_receipt_file(receipt_dir, final)
            logger.info(
                "erasure_request_completed",
                extra={
                    "event": "erasure_request_completed",
                    "request_id": final.request_id,
                    "subject_sha256_prefix": hash_prefix,
                    "outcome": "already_erased",
                },
            )
            return final
        detail = sanitize_error_detail(str(exc), record.subject_id)
        final = queue.finalize(
            record.request_id,
            state=STATE_FAILED,
            receipt={
                "kind": "error",
                "error_class": "subject_not_found",
                "error_detail": detail,
            },
            error_class="subject_not_found",
            error_detail=detail,
        )
        logger.warning(
            "erasure_request_failed",
            extra={
                "event": "erasure_request_failed",
                "request_id": final.request_id,
                "subject_sha256_prefix": hash_prefix,
                "error_class": "subject_not_found",
            },
        )
        return final
    except Exception as exc:  # noqa: BLE001 — fail-closed: FAILED, never PENDING
        error_class = type(exc).__name__
        detail = sanitize_error_detail(str(exc), record.subject_id)
        final = queue.finalize(
            record.request_id,
            state=STATE_FAILED,
            receipt={
                "kind": "error",
                "error_class": error_class,
                "error_detail": detail,
            },
            error_class=error_class,
            error_detail=detail,
        )
        logger.warning(
            "erasure_request_failed",
            extra={
                "event": "erasure_request_failed",
                "request_id": final.request_id,
                "subject_sha256_prefix": hash_prefix,
                "error_class": error_class,
            },
        )
        return final

    receipt = json.loads(result.model_dump_json())
    state = (
        STATE_DEFERRED if isinstance(result, ErasureDeferredReceipt) else STATE_COMPLETED
    )
    final = queue.finalize(record.request_id, state=state, receipt=receipt)
    _write_receipt_file(receipt_dir, final)
    logger.info(
        "erasure_request_completed" if state == STATE_COMPLETED else "erasure_request_deferred",
        extra={
            "event": (
                "erasure_request_completed"
                if state == STATE_COMPLETED
                else "erasure_request_deferred"
            ),
            "request_id": final.request_id,
            "subject_sha256_prefix": hash_prefix,
            "state": state,
        },
    )
    return final
