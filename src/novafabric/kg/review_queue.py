"""Tier 3 entity resolution — SQLite-backed human review queue.

When the extractor finds an entity it cannot confidently alias
(confidence < threshold), it writes a ``ReviewItem`` here for human triage.

The queue is fully synchronous and thread-safe (threading.RLock).
No Postgres / asyncpg dependency — SQLite is stdlib.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from novafabric._sqlite_util import connect_sqlite

logger = logging.getLogger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS entity_review_queue (
    item_id           TEXT PRIMARY KEY,
    alias             TEXT NOT NULL,
    entity_type       TEXT NOT NULL,
    context           TEXT NOT NULL DEFAULT '{}',
    suggested_canonical TEXT,
    confidence        REAL NOT NULL DEFAULT 0.0,
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        TEXT NOT NULL,
    resolved_at       TEXT,
    resolved_by       TEXT
)
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO entity_review_queue
    (item_id, alias, entity_type, context, suggested_canonical,
     confidence, status, created_at, resolved_at, resolved_by)
VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)
"""


class ReviewItem(BaseModel):
    item_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    alias: str
    entity_type: str
    context: dict[str, Any] = Field(default_factory=dict)
    suggested_canonical: str | None = None
    confidence: float = 0.0
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class HumanReviewQueueWriter:
    """SQLite-backed queue for low-confidence entity aliases needing human review.

    Usage::

        q = HumanReviewQueueWriter(db_path=Path(".nova/review_queue.db"))
        q.enqueue(ReviewItem(alias="mystery-model", entity_type="model",
                             confidence=0.4, context={"capsule_id": "cap-1"}))
        pending = q.list_pending()
        q.approve(pending[0].item_id, canonical="gpt-4o", resolved_by="alice")
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # Internal setup
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(_CREATE_SQL)
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """Return the shared connection (create on first call).

        Called under self._lock.
        """
        if self._conn is None:
            self._conn = connect_sqlite(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ReviewItem:
        import json as _json

        ctx: dict[str, Any] = {}
        raw_ctx = row["context"]
        if raw_ctx:
            try:
                ctx = _json.loads(raw_ctx)
            except Exception:
                ctx = {}

        return ReviewItem(
            item_id=row["item_id"],
            alias=row["alias"],
            entity_type=row["entity_type"],
            context=ctx,
            suggested_canonical=row["suggested_canonical"],
            confidence=float(row["confidence"]),
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"])
            if row["resolved_at"]
            else None,
            resolved_by=row["resolved_by"],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, item: ReviewItem) -> None:
        """Insert *item* into the queue.  Duplicate item_ids are silently ignored
        (INSERT OR IGNORE).
        """
        import json as _json

        with self._lock:
            conn = self._get_conn()
            conn.execute(
                _INSERT_SQL,
                (
                    item.item_id,
                    item.alias,
                    item.entity_type,
                    _json.dumps(item.context),
                    item.suggested_canonical,
                    item.confidence,
                    item.created_at.isoformat(),
                ),
            )
            conn.commit()
            logger.debug(
                "ReviewQueue: enqueued %r (entity_type=%s, confidence=%.3f)",
                item.alias,
                item.entity_type,
                item.confidence,
            )

    def list_pending(self, limit: int | None = None) -> list[ReviewItem]:
        """Return items with status='pending', ordered by created_at asc.

        ``limit`` (optional, additive) bounds the read at the SQL layer —
        O(limit) rows materialised instead of the whole queue (ADR-0199).
        ``None`` keeps the historical return-everything behavior.
        """
        with self._lock:
            conn = self._get_conn()
            sql = (
                "SELECT * FROM entity_review_queue WHERE status = 'pending' "
                "ORDER BY created_at ASC"
            )
            params: tuple[int, ...] = ()
            if limit is not None:
                sql += " LIMIT ?"
                params = (int(limit),)
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_item(r) for r in rows]

    def count_pending(self) -> int:
        """Return the total number of items with status='pending'."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM entity_review_queue WHERE status = 'pending'"
            ).fetchone()
            return int(row[0])

    def approve(self, item_id: str, canonical: str, resolved_by: str) -> None:
        """Mark *item_id* as approved and record the canonical name.

        Also updates ``suggested_canonical`` so callers can read back the
        resolution without a separate query.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE entity_review_queue "
                "SET status = 'approved', suggested_canonical = ?, "
                "    resolved_at = ?, resolved_by = ? "
                "WHERE item_id = ? AND status = 'pending'",
                (canonical, now, resolved_by, item_id),
            )
            conn.commit()
            logger.debug("ReviewQueue: approved item_id=%s → %r", item_id, canonical)

    def reject(self, item_id: str, resolved_by: str) -> None:
        """Mark *item_id* as rejected."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE entity_review_queue "
                "SET status = 'rejected', resolved_at = ?, resolved_by = ? "
                "WHERE item_id = ? AND status = 'pending'",
                (now, resolved_by, item_id),
            )
            conn.commit()
            logger.debug("ReviewQueue: rejected item_id=%s by %s", item_id, resolved_by)

    def stats(self) -> dict[str, int]:
        """Return ``{"pending": N, "approved": N, "rejected": N}``."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM entity_review_queue GROUP BY status"
            ).fetchall()
            result: dict[str, int] = {"pending": 0, "approved": 0, "rejected": 0}
            for row in rows:
                if row["status"] in result:
                    result[row["status"]] = int(row["cnt"])
            return result

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
