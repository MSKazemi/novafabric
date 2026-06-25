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
"""SQLite index for redaction subject lookups (cap-001)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS redaction_subject_idx (
    subject_id_hmac   TEXT NOT NULL,
    capsule_id        TEXT NOT NULL,
    field_path        TEXT NOT NULL,
    legal_basis       TEXT NOT NULL,
    redacted_at_utc   TEXT NOT NULL,
    PRIMARY KEY (subject_id_hmac, capsule_id, field_path)
);

CREATE INDEX IF NOT EXISTS idx_redaction_subject_hmac
    ON redaction_subject_idx (subject_id_hmac);

CREATE INDEX IF NOT EXISTS idx_redaction_capsule
    ON redaction_subject_idx (capsule_id);
"""


@dataclass(frozen=True)
class SubjectRedactionRecord:
    """A single row from the redaction_subject_idx table."""

    subject_id_hmac: str
    capsule_id: str
    field_path: str
    legal_basis: str
    redacted_at_utc: str


class RedactionSubjectIndex:
    """Append-only SQLite index for subject-to-capsule redaction lookups.

    Supports GDPR Art.17 erasure proof generation: given a subject_id_hmac,
    returns all capsules and fields that were redacted for that subject.

    Uses parameterised queries exclusively — no f-string SQL.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "RedactionSubjectIndex":
        self.open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(
        self,
        subject_id_hmac: str,
        capsule_id: str,
        field_path: str,
        legal_basis: str,
        redacted_at_utc: str,
    ) -> None:
        """Insert a redaction record.  Duplicate primary key is silently ignored."""
        if self._conn is None:
            raise RuntimeError("RedactionSubjectIndex is not open")
        self._conn.execute(
            """
            INSERT OR IGNORE INTO redaction_subject_idx
                (subject_id_hmac, capsule_id, field_path, legal_basis, redacted_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (subject_id_hmac, capsule_id, field_path, legal_basis, redacted_at_utc),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def lookup_subject(self, subject_id_hmac: str) -> list[SubjectRedactionRecord]:
        """Return all redaction records for the given HMAC-ed subject ID."""
        if self._conn is None:
            raise RuntimeError("RedactionSubjectIndex is not open")
        rows = self._conn.execute(
            """
            SELECT subject_id_hmac, capsule_id, field_path, legal_basis, redacted_at_utc
            FROM redaction_subject_idx
            WHERE subject_id_hmac = ?
            ORDER BY redacted_at_utc
            """,
            (subject_id_hmac,),
        ).fetchall()
        return [SubjectRedactionRecord(*row) for row in rows]

    def lookup_capsule(self, capsule_id: str) -> list[SubjectRedactionRecord]:
        """Return all redaction records for a given capsule."""
        if self._conn is None:
            raise RuntimeError("RedactionSubjectIndex is not open")
        rows = self._conn.execute(
            """
            SELECT subject_id_hmac, capsule_id, field_path, legal_basis, redacted_at_utc
            FROM redaction_subject_idx
            WHERE capsule_id = ?
            ORDER BY redacted_at_utc
            """,
            (capsule_id,),
        ).fetchall()
        return [SubjectRedactionRecord(*row) for row in rows]

    def count(self) -> int:
        if self._conn is None:
            raise RuntimeError("RedactionSubjectIndex is not open")
        row = self._conn.execute(
            "SELECT COUNT(*) FROM redaction_subject_idx"
        ).fetchone()
        return int(row[0])
