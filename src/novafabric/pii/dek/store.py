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
"""DEK store: per-subject AES-256-GCM key lifecycle for GDPR Art.17 crypto-shredding.

Design per ADR-0069:
- One AES-256-GCM DEK per data subject, generated at first PII detection.
- DEKs are stored in a dedicated SQLite database (dek.db) outside the capsule store.
- Erasure destroys the DEK (overwrite with os.urandom(32), then DELETE) so ciphertext
  in capsule records becomes permanently unrecoverable without touching WORM storage.
- Art.17(3)(b) retention window: erasure within the configured retention period produces
  an ErasureDeferredReceipt rather than an ErasureReceipt.

Security:
- No plaintext DEK is ever logged or returned to the caller beyond the initial creation.
- SQLite write serialisation prevents concurrent corruption.
- The dek.db file must be protected with filesystem permissions (0600).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DataSubjectDEK(BaseModel):
    """A per-subject AES-256-GCM Data Encryption Key record."""

    subject_id: str = Field(..., description="Data subject identifier (opaque string)")
    dek_hex: str = Field(
        ...,
        description="32-byte AES-256 key encoded as lowercase hex (64 hex chars)",
    )
    created_at: datetime = Field(..., description="UTC timestamp of key creation")
    tenant_id: str = Field(default="default", description="Tenant namespace")

    model_config = {"frozen": True}


class DEKSubjectRecord(BaseModel):
    """Key-free view of a DEK row — subject identity and creation time only.

    Used by read-only status reporting (``nova pii status``).  Deliberately
    excludes ``dek_hex`` so key material never leaves the store for queries.
    """

    subject_id: str = Field(..., description="Data subject identifier (opaque string)")
    tenant_id: str = Field(default="default", description="Tenant namespace")
    created_at: datetime = Field(..., description="UTC timestamp of key creation")

    model_config = {"frozen": True}


class ErasureReceipt(BaseModel):
    """Confirmation that a DEK has been destroyed (GDPR Art.17 erasure complete)."""

    subject_id: str = Field(..., description="Data subject whose DEK was destroyed")
    erased_at: datetime = Field(..., description="UTC timestamp of DEK destruction")
    capsule_ids_affected: list[str] = Field(
        default_factory=list,
        description="Capsule IDs whose ciphertext is now permanently unrecoverable",
    )
    method: str = Field(
        default="aes-256-gcm-dek-destruction",
        description="Erasure method identifier per ADR-0069",
    )
    legal_basis: str = Field(
        default="GDPR Art.17",
        description="Legal basis for erasure",
    )

    model_config = {"frozen": True}


class ErasureDeferredReceipt(BaseModel):
    """Erasure deferred because the capsule is within the Art.17(3)(b) retention window."""

    subject_id: str = Field(..., description="Data subject whose erasure was deferred")
    requested_at: datetime = Field(..., description="UTC timestamp of the erasure request")
    earliest_erasure_at: datetime = Field(
        ...,
        description=(
            "Earliest datetime when erasure may proceed per Art.17(3)(b) "
            "and NOVA_AI_ACT_RETENTION_MONTHS"
        ),
    )
    reason: str = Field(
        ...,
        description="Human-readable reason why erasure was deferred",
    )
    retention_months: int = Field(
        ...,
        description="Configured retention window in months",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# DEKStore
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS data_subject_deks (
    subject_id  TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT 'default',
    dek_hex     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


class DEKStore:
    """SQLite-backed per-subject DEK store.

    Thread-safety: SQLite in WAL mode with ``check_same_thread=False`` is used;
    concurrent readers are safe and concurrent writers serialise naturally via
    SQLite's WAL write lock.  The ``isolation_level=None`` (autocommit) is NOT
    used — each mutation is wrapped in an explicit transaction to allow atomic
    overwrite-then-delete during erasure.

    The database file should be protected with filesystem permissions 0600.
    """

    def __init__(self, db_path: Path) -> None:
        """Open (or create) the DEK store at *db_path*."""
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._bootstrap()

    def _bootstrap(self) -> None:
        """Create schema if not present."""
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create_dek(
        self,
        subject_id: str,
        tenant_id: str = "default",
    ) -> DataSubjectDEK:
        """Return the existing DEK for *subject_id*, or generate and store a new one.

        Idempotent: repeated calls with the same *subject_id* return the same DEK
        (as long as the DEK has not been erased).

        Args:
            subject_id: Data subject identifier.
            tenant_id: Tenant namespace (default ``"default"``).

        Returns:
            The :class:`DataSubjectDEK` for this subject.

        Raises:
            RuntimeError: If the database is in an inconsistent state.
        """
        with self._lock:
            # Generate a candidate key inside the lock.
            candidate_key = os.urandom(32)
            candidate_hex = candidate_key.hex()
            candidate_at = datetime.now(timezone.utc)

            with self._conn:
                # Use INSERT OR IGNORE so concurrent processes don't raise IntegrityError;
                # the loser's INSERT is silently discarded and the winner's row persists.
                self._conn.execute(
                    "INSERT OR IGNORE INTO data_subject_deks "
                    "(subject_id, tenant_id, dek_hex, created_at) VALUES (?, ?, ?, ?)",
                    (subject_id, tenant_id, candidate_hex, candidate_at.isoformat()),
                )
                # Always read back the persisted row (handles both insert and race-loss).
                cur = self._conn.execute(
                    "SELECT subject_id, tenant_id, dek_hex, created_at "
                    "FROM data_subject_deks WHERE subject_id = ?",
                    (subject_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(
                        f"DEK insertion succeeded but row not found for subject_id={subject_id!r}. "
                        "This is a bug in DEKStore."
                    )

            created = row[2] == candidate_hex

        # Log only if this thread created the row.
        if created:
            logger.info(
                "dek_created",
                extra={
                    "event": "dek_created",
                    "subject_id": subject_id,
                    "tenant_id": tenant_id,
                },
            )

        return DataSubjectDEK(
            subject_id=row[0],
            tenant_id=row[1],
            dek_hex=row[2],
            created_at=datetime.fromisoformat(row[3]),
        )

    def get_dek(self, subject_id: str) -> DataSubjectDEK | None:
        """Return the DEK for *subject_id*, or ``None`` if erased or not found.

        Args:
            subject_id: Data subject identifier.

        Returns:
            :class:`DataSubjectDEK` if found; ``None`` otherwise.
        """
        cur = self._conn.execute(
            "SELECT subject_id, tenant_id, dek_hex, created_at "
            "FROM data_subject_deks WHERE subject_id = ?",
            (subject_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return DataSubjectDEK(
            subject_id=row[0],
            tenant_id=row[1],
            dek_hex=row[2],
            created_at=datetime.fromisoformat(row[3]),
        )

    def list_subjects(self) -> list[DEKSubjectRecord]:
        """Return all subjects with an active (non-erased) DEK, without key material.

        Read-only.  Erased subjects do not appear (their rows are deleted by
        :meth:`erase_subject`).

        Returns:
            List of :class:`DEKSubjectRecord` ordered by ``subject_id``.
        """
        cur = self._conn.execute(
            "SELECT subject_id, tenant_id, created_at "
            "FROM data_subject_deks ORDER BY subject_id",
        )
        return [
            DEKSubjectRecord(
                subject_id=row[0],
                tenant_id=row[1],
                created_at=datetime.fromisoformat(row[2]),
            )
            for row in cur.fetchall()
        ]

    def erase_subject(
        self,
        subject_id: str,
        capsule_ids: list[str],
        retention_months: int = 6,
    ) -> ErasureReceipt | ErasureDeferredReceipt:
        """Destroy the DEK for *subject_id* (GDPR Art.17 crypto-shredding).

        If the DEK was created within the Art.17(3)(b) retention window
        (``retention_months``), returns an :class:`ErasureDeferredReceipt`
        without modifying the DEK.

        Otherwise: overwrites the DEK bytes with ``os.urandom(32)``, then
        deletes the row, and returns an :class:`ErasureReceipt`.

        Args:
            subject_id: Data subject identifier.
            capsule_ids: List of capsule IDs associated with this subject
                         (recorded in the receipt; no capsule data is touched).
            retention_months: Minimum retention window in months per
                              ``NOVA_AI_ACT_RETENTION_MONTHS`` (default 6).

        Returns:
            :class:`ErasureReceipt` on success, or
            :class:`ErasureDeferredReceipt` if within the retention window.

        Raises:
            KeyError: If no DEK exists for *subject_id*.
        """
        now = datetime.now(timezone.utc)

        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT subject_id, tenant_id, dek_hex, created_at "
                "FROM data_subject_deks WHERE subject_id = ?",
                (subject_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(
                    f"No DEK found for subject_id={subject_id!r}. "
                    "Either the subject was never registered or the DEK has already been erased."
                )

            created_at = datetime.fromisoformat(row[3])
            retention_cutoff = created_at + timedelta(days=retention_months * 30)

            if now < retention_cutoff:
                # Within retention window — defer erasure.
                reason = (
                    f"Capsule created at {created_at.isoformat()} is within the "
                    f"{retention_months}-month Art.17(3)(b) retention window "
                    f"(EU AI Act Art.12). Earliest erasure: {retention_cutoff.isoformat()}."
                )
                logger.info(
                    "dek_erasure_deferred",
                    extra={
                        "event": "dek_erasure_deferred",
                        "subject_id": subject_id,
                        "earliest_erasure_at": retention_cutoff.isoformat(),
                        "retention_months": retention_months,
                    },
                )
                return ErasureDeferredReceipt(
                    subject_id=subject_id,
                    requested_at=now,
                    earliest_erasure_at=retention_cutoff,
                    reason=reason,
                    retention_months=retention_months,
                )

            # Overwrite the DEK bytes before deleting (belt-and-suspenders).
            overwrite_hex = os.urandom(32).hex()
            self._conn.execute(
                "UPDATE data_subject_deks SET dek_hex = ? WHERE subject_id = ?",
                (overwrite_hex, subject_id),
            )
            self._conn.execute(
                "DELETE FROM data_subject_deks WHERE subject_id = ?",
                (subject_id,),
            )

        logger.info(
            "dek_erased",
            extra={
                "event": "dek_erased",
                "subject_id": subject_id,
                "capsule_count": len(capsule_ids),
                "method": "aes-256-gcm-dek-destruction",
            },
        )
        return ErasureReceipt(
            subject_id=subject_id,
            erased_at=now,
            capsule_ids_affected=list(capsule_ids),
            method="aes-256-gcm-dek-destruction",
            legal_basis="GDPR Art.17",
        )

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def open_dek_store(home: Path | None = None) -> DEKStore:
    """Open the DEK store using ``$NOVAFABRIC_HOME/dek.db`` (or *home*/dek.db).

    Args:
        home: Override for the NovaFabric home directory.  If ``None``,
              uses ``NOVAFABRIC_HOME`` env var or ``~/.novafabric``.

    Returns:
        A ready :class:`DEKStore` instance.
    """
    if home is None:
        home = Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))
    db_path = home / "dek.db"
    return DEKStore(db_path)
