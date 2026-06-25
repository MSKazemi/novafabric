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
"""SQLite store for Incident records (ADR-0088).

Mirrors the local-first storage pattern of the registry /
:class:`~novafabric.compliance.tool_permission.index.PermissionEventIndex`:
parameterised queries only, schema created on open, lives under
``NOVAFABRIC_HOME``.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from novafabric._paths import nova_home
from novafabric.compliance.incident.models import (
    Incident,
    IncidentNotFoundError,
    IncidentStatus,
)

_DDL = """
CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,
    severity    TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    aware_at    TEXT,
    raw_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status);
"""


def incidents_db_path() -> Path:
    """Incident store path: ``$NOVAFABRIC_HOME/incidents.db``.

    Override with ``NOVAFABRIC_INCIDENTS_DB_PATH``.
    """
    env = os.environ.get("NOVAFABRIC_INCIDENTS_DB_PATH")
    return Path(env) if env else nova_home() / "incidents.db"


class IncidentStore:
    """Local-first SQLite persistence for :class:`Incident` records."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else incidents_db_path()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open the database and create the schema if needed."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.executescript(_DDL)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "IncidentStore":
        self.open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("IncidentStore is not open; call open() first")
        return self._conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create(self, incident: Incident) -> Incident:
        """Persist a new incident; duplicate id raises ``ValueError``."""
        conn = self._require_conn()
        try:
            conn.execute(
                """
                INSERT INTO incidents
                    (id, title, status, severity, occurred_at, aware_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                _row_of(incident),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"incident id already exists: {incident.id}") from exc
        conn.commit()
        return incident

    def save(self, incident: Incident) -> Incident:
        """Update an existing incident (used after lifecycle transitions)."""
        conn = self._require_conn()
        cur = conn.execute(
            """
            UPDATE incidents
            SET title = ?, status = ?, severity = ?, occurred_at = ?,
                aware_at = ?, raw_json = ?
            WHERE id = ?
            """,
            (*_row_of(incident)[1:], incident.id),
        )
        if cur.rowcount == 0:
            raise IncidentNotFoundError(incident.id)
        conn.commit()
        return incident

    def transition(
        self, incident_id: str, new_status: IncidentStatus, at: datetime | None = None
    ) -> Incident:
        """Apply a forward-only lifecycle transition and persist it."""
        updated = self.get(incident_id).transitioned(new_status, at=at)
        return self.save(updated)

    def attach_attestation(
        self, incident_id: str, ref: str, at: datetime | None = None
    ) -> Incident:
        """Attach a ReperformanceAttestation reference (audited event)."""
        updated = self.get(incident_id).with_attestation(ref, at=at)
        return self.save(updated)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, incident_id: str) -> Incident:
        """Return the incident or raise :class:`IncidentNotFoundError`."""
        row = self._require_conn().execute(
            "SELECT raw_json FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            raise IncidentNotFoundError(incident_id)
        return Incident.model_validate_json(row[0])

    def list_all(self) -> list[Incident]:
        """All incidents, newest occurrence first."""
        rows = self._require_conn().execute(
            "SELECT raw_json FROM incidents ORDER BY occurred_at DESC"
        ).fetchall()
        return [Incident.model_validate_json(r[0]) for r in rows]


def _row_of(incident: Incident) -> tuple[Any, ...]:
    return (
        incident.id,
        incident.title,
        incident.status.value,
        incident.severity.value,
        incident.occurred_at.isoformat(),
        incident.aware_at.isoformat() if incident.aware_at else None,
        incident.model_dump_json(),
    )
