from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from .worm import IntegrityResult, WormEntry, WormReceipt

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS worm_capsules (
    capsule_id TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    locked_until TEXT NOT NULL,
    hold_ids TEXT NOT NULL DEFAULT ''
);
"""


class LocalWormAdapter:
    """SQLite append-only WORM adapter. Dev/test only — not true WORM.

    A local filesystem administrator with direct SQLite access can modify
    this file. Do not use for production compliance purposes.
    """

    def __init__(self, db_path: Path) -> None:
        self._db = db_path
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(_CREATE_SQL)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db)

    def put(self, capsule_id: str, data: bytes, retention_days: int) -> WormReceipt:
        locked_until = datetime.now(tz=timezone.utc) + timedelta(days=retention_days)
        sha = hashlib.sha256(data).hexdigest()
        with self._conn() as conn:
            # INSERT OR IGNORE preserves the original data if capsule_id already exists
            conn.execute(
                "INSERT OR IGNORE INTO worm_capsules"
                " (capsule_id, data, sha256, locked_until) VALUES (?,?,?,?)",
                (capsule_id, data, sha, locked_until.isoformat()),
            )
        return WormReceipt(
            capsule_id=capsule_id,
            backend_type="local",
            locked_until=locked_until,
            backend_confirmation_token=sha[:16],
        )

    def get(self, capsule_id: str) -> bytes:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM worm_capsules WHERE capsule_id=?", (capsule_id,)
            ).fetchone()
        if row is None:
            raise KeyError(capsule_id)
        return cast(bytes, row[0])

    def lock(self, capsule_id: str, hold_id: str) -> None:
        if "," in hold_id:
            raise ValueError(f"hold_id must not contain commas: {hold_id!r}")
        with self._conn() as conn:
            result = conn.execute(
                """UPDATE worm_capsules
                   SET hold_ids = CASE WHEN hold_ids = '' THEN ? ELSE hold_ids || ',' || ? END
                   WHERE capsule_id = ?""",
                (hold_id, hold_id, capsule_id),
            )
            if result.rowcount == 0:
                raise KeyError(capsule_id)

    def list(self, prefix: str | None = None) -> list[WormEntry]:
        with self._conn() as conn:
            if prefix:
                rows = conn.execute(
                    "SELECT capsule_id, locked_until, length(data)"
                    " FROM worm_capsules WHERE capsule_id LIKE ?",
                    (f"{prefix}%",),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT capsule_id, locked_until, length(data) FROM worm_capsules"
                ).fetchall()
        return [
            WormEntry(
                capsule_id=r[0],
                locked_until=datetime.fromisoformat(r[1]),
                size_bytes=r[2],
            )
            for r in rows
        ]

    def verify_integrity(self, capsule_id: str) -> IntegrityResult:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data, sha256 FROM worm_capsules WHERE capsule_id=?", (capsule_id,)
            ).fetchone()
        if row is None:
            return IntegrityResult(capsule_id=capsule_id, ok=False, error="not found")
        data, stored_sha = cast(bytes, row[0]), row[1]
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != stored_sha:
            return IntegrityResult(capsule_id=capsule_id, ok=False, error="sha256 mismatch")
        return IntegrityResult(capsule_id=capsule_id, ok=True)
