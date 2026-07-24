"""Comments on registry assets — ADR-0121 P3 (experimental).

Capsule comments live in the capsule's append-only ``comments.jsonl``.
Registry assets have no capsule, so their comments live in the registry's
``asset_comments`` table instead.

**The storage differs; the semantics do not.** This module deliberately
mirrors :mod:`novafabric.capsule.comments` function-for-function
(``read_comments`` / ``append_comment``), returns the same :class:`Comment`
records, and preserves the same invariants:

- **append-only** — rows are never ``UPDATE``d or ``DELETE``d. An edit is a
  reply (``in_reply_to``); a delete is a tombstone row. The table is a log
  that happens to be in SQLite, not mutable state.
- **reader-side views are shared** — ``apply_tombstones`` and
  ``resolve_thread`` from the capsule module operate on these records
  unchanged, because they are the same records.

Keeping the two backends semantically identical is the point: a comment
should not mean something different because of where it is stored, and the
CLI should not need to branch on subject kind beyond choosing a backend.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from novafabric.capsule.comments import Comment

#: Table owned by this module (created in ``registry.store.init_schema``).
TABLE = "asset_comments"

_COLUMNS = (
    "comment_id",
    "subject",
    "subject_kind",
    "author",
    "created_at",
    "body",
    "in_reply_to",
    "tombstone",
    "redaction_applied",
)


def _row_to_comment(row: sqlite3.Row | tuple[Any, ...]) -> Comment:
    data = dict(zip(_COLUMNS, tuple(row), strict=True))
    # SQLite has no bool; the JSONL side stores real booleans, so normalise
    # here rather than leaking an int into a Comment that came from a table.
    data["tombstone"] = bool(data["tombstone"])
    data["redaction_applied"] = bool(data["redaction_applied"])
    if data["in_reply_to"] is None:
        data.pop("in_reply_to")
    return Comment.model_validate(data)


def append_comment(conn: sqlite3.Connection, comment: Comment) -> None:
    """Append one comment. The **only** write operation (ADR-0121 D3).

    There is deliberately no update or delete counterpart — the append-only
    invariant is enforced by the absence of the API, exactly as it is for the
    JSONL backend.
    """
    conn.execute(
        f"INSERT INTO {TABLE} "  # noqa: S608 — TABLE is a module constant
        "(comment_id, subject, subject_kind, author, created_at, body, "
        " in_reply_to, tombstone, redaction_applied) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            comment.comment_id,
            comment.subject,
            str(getattr(comment.subject_kind, "value", comment.subject_kind)),
            comment.author,
            comment.created_at,
            comment.body,
            comment.in_reply_to,
            int(comment.tombstone),
            int(comment.redaction_applied),
        ),
    )
    conn.commit()


def read_comments(conn: sqlite3.Connection, subject: str) -> list[Comment]:
    """Every comment on *subject*, in write order.

    Ordered by ``created_at`` then ``comment_id``: the timestamp alone is not
    a total order (two comments can share a second), and an unstable order
    would make ``resolve_thread`` output vary between reads.
    """
    cur = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM {TABLE} "  # noqa: S608
        "WHERE subject = ? ORDER BY created_at, comment_id",
        (subject,),
    )
    return [_row_to_comment(row) for row in cur.fetchall()]


def read_all_comments(conn: sqlite3.Connection) -> list[Comment]:
    """Every asset comment, for the ``--all`` raw audit view."""
    cur = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM {TABLE} "  # noqa: S608
        "ORDER BY created_at, comment_id"
    )
    return [_row_to_comment(row) for row in cur.fetchall()]


def default_db_path() -> Path:
    """Registry DB path, resolved lazily so imports stay cheap."""
    from novafabric.registry.store import get_db_path

    return get_db_path()
