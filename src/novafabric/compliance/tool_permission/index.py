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
"""SQLite index for ToolPermissionEvent records (cap-004)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .models import ToolPermissionEvent

_DDL = """
CREATE TABLE IF NOT EXISTS tool_permission_idx (
    event_id          TEXT PRIMARY KEY,
    capsule_id        TEXT NOT NULL,
    tool_name         TEXT NOT NULL,
    tool_id           TEXT NOT NULL,
    policy_id         TEXT NOT NULL,
    permission_level  TEXT NOT NULL,
    decision          TEXT NOT NULL,
    authorising_identity TEXT NOT NULL,
    human_approval_required INTEGER NOT NULL,
    human_approval_obtained INTEGER,
    approval_principal TEXT,
    decision_latency_ms INTEGER NOT NULL,
    capsule_timestamp_utc TEXT NOT NULL,
    raw_json          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_permission_tool_decision
    ON tool_permission_idx (tool_name, decision);

CREATE INDEX IF NOT EXISTS idx_tool_permission_capsule
    ON tool_permission_idx (capsule_id);
"""


class PermissionEventIndex:
    """Append-only SQLite index of :class:`ToolPermissionEvent` records.

    Uses parameterised queries exclusively — no f-string SQL.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
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

    def __enter__(self) -> "PermissionEventIndex":
        self.open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record(self, event: ToolPermissionEvent) -> None:
        """Insert *event* into the index.  Duplicate event_id is silently ignored."""
        if self._conn is None:
            raise RuntimeError("PermissionEventIndex is not open; call open() first")
        self._conn.execute(
            """
            INSERT OR IGNORE INTO tool_permission_idx (
                event_id, capsule_id, tool_name, tool_id, policy_id,
                permission_level, decision, authorising_identity,
                human_approval_required, human_approval_obtained,
                approval_principal, decision_latency_ms, capsule_timestamp_utc,
                raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.capsule_id,
                event.tool_name,
                event.tool_id,
                event.policy_id,
                event.permission_level,
                event.decision,
                event.authorising_identity,
                int(event.human_approval_required),
                (
                    int(event.human_approval_obtained)
                    if event.human_approval_obtained is not None
                    else None
                ),
                event.approval_principal,
                event.decision_latency_ms,
                event.capsule_timestamp_utc,
                event.model_dump_json(),
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def query_by_capsule(self, capsule_id: str) -> list[ToolPermissionEvent]:
        """Return all events for a given capsule."""
        if self._conn is None:
            raise RuntimeError("PermissionEventIndex is not open; call open() first")
        rows = self._conn.execute(
            "SELECT raw_json FROM tool_permission_idx WHERE capsule_id = ?",
            (capsule_id,),
        ).fetchall()
        return [ToolPermissionEvent.model_validate_json(row[0]) for row in rows]

    def query_by_tool_and_decision(
        self, tool_name: str, decision: str
    ) -> list[ToolPermissionEvent]:
        """Return events matching tool_name AND decision (uses B-tree index)."""
        if self._conn is None:
            raise RuntimeError("PermissionEventIndex is not open; call open() first")
        rows = self._conn.execute(
            """
            SELECT raw_json FROM tool_permission_idx
            WHERE tool_name = ? AND decision = ?
            """,
            (tool_name, decision),
        ).fetchall()
        return [ToolPermissionEvent.model_validate_json(row[0]) for row in rows]

    def count(self) -> int:
        """Return the total number of indexed events."""
        if self._conn is None:
            raise RuntimeError("PermissionEventIndex is not open; call open() first")
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tool_permission_idx"
        ).fetchone()
        return int(row[0])
