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

"""Deployment-label registry operations (ADR-0113).

Extends the existing local SQLite registry with one **additive** table,
``asset_label_history`` — an append-only audit log of label moves. The
current pointer of a label is a projection (the newest row per
``(asset_name, label)``); it is always reconstructible from history alone.
SQLite triggers enforce append-only at the storage layer: ``UPDATE`` and
``DELETE`` on the table abort.

Invariants (ADR-0113 D1–D4; spec ``the private design/spec/asset-labels-v0.md``):

* **Append-only moves** — :func:`set_label` only ever inserts; prior rows
  are never mutated or deleted.
* **Fail-closed targets** — setting a label to a version that does not
  exist in the registry errors and writes no row.
* **Reserved ``latest``** — auto-maintained (always the highest registered
  version), never user-settable.
* **Resolution freeze** — :func:`resolve_asset_ref` resolves a
  ``<type>:<name>@<label-or-version>`` reference to the concrete immutable
  version + content hash and returns the capsule-ready
  :class:`~novafabric.spec.asset_labels.ResolvedAssetRef` record. Replay
  uses the frozen version, never the label.
"""

from __future__ import annotations

import getpass
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.capture._ulid import new_ulid
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.asset_labels import (
    RESERVED_LABEL_LATEST,
    AssetLabelMove,
    ResolvedAssetRef,
    validate_label_name,
)


class LabelError(Exception):
    """Base class for deployment-label failures."""


class LabelNotFoundError(LabelError):
    """No label matches the requested ``(asset_name, label)``."""


class LabelTargetNotFoundError(LabelError):
    """The target version does not exist in the registry (fail-closed)."""


class ReservedLabelError(LabelError):
    """The label is registry-maintained and read-only to the user."""


class ProtectedLabelError(LabelError):
    """The label is protected (ADR-0114): direct set refused; use maker-checker."""


class UnresolvableRefError(LabelError):
    """An ``<asset_type>:<asset_name>@<selector>`` reference cannot resolve."""


_LABEL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS asset_label_history (
    move_id          TEXT PRIMARY KEY,
    schema_version   TEXT NOT NULL,
    label            TEXT NOT NULL,
    asset_name       TEXT NOT NULL,
    asset_type       TEXT,
    target_version   TEXT NOT NULL,
    previous_version TEXT,
    content_hash     TEXT,
    reason           TEXT,
    moved_at         TEXT NOT NULL,
    moved_by         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_label_history_key
    ON asset_label_history(asset_name, label, moved_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_asset_label_history_no_update
    BEFORE UPDATE ON asset_label_history
    BEGIN SELECT RAISE(ABORT, 'asset_label_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_asset_label_history_no_delete
    BEFORE DELETE ON asset_label_history
    BEGIN SELECT RAISE(ABORT, 'asset_label_history is append-only'); END;
"""


def ensure_label_schema(conn: sqlite3.Connection) -> None:
    """Create the additive label-history table (idempotent, additive-only)."""
    conn.executescript(_LABEL_SCHEMA_SQL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open(db_path: Path | None) -> sqlite3.Connection:
    conn = get_connection(db_path)
    init_schema(conn)
    ensure_label_schema(conn)
    return conn


def _asset_version_row(
    conn: sqlite3.Connection,
    asset_name: str,
    version: str,
    asset_type: str | None = None,
) -> sqlite3.Row | None:
    query = "SELECT * FROM assets WHERE name = ? AND version = ?"
    params: list[str] = [asset_name, version]
    if asset_type:
        query += " AND asset_type = ?"
        params.append(asset_type)
    row: sqlite3.Row | None = conn.execute(query, params).fetchone()
    return row


def _latest_version_row(
    conn: sqlite3.Connection,
    asset_name: str,
    asset_type: str | None = None,
) -> sqlite3.Row | None:
    """The highest registered version of an asset (the ``latest`` target).

    Managed integer versions (ADR-0112 prompts) order numerically; assets
    without any integer version fall back to the newest ``created_at`` row.
    """
    query = "SELECT * FROM assets WHERE name = ?"
    params: list[str] = [asset_name]
    if asset_type:
        query += " AND asset_type = ?"
        params.append(asset_type)
    rows: list[sqlite3.Row] = conn.execute(query, params).fetchall()
    if not rows:
        return None
    integer_rows: list[tuple[int, sqlite3.Row]] = []
    for row in rows:
        try:
            integer_rows.append((int(row["version"]), row))
        except (TypeError, ValueError):
            continue
    if integer_rows:
        return max(integer_rows, key=lambda pair: pair[0])[1]
    return max(rows, key=lambda r: str(r["created_at"]))


def _content_hash(row: sqlite3.Row) -> str | None:
    try:
        spec: dict[str, Any] = json.loads(row["spec_json"])
    except (TypeError, ValueError):
        return None
    value = spec.get("content_hash")
    return value if isinstance(value, str) else None


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = AssetLabelMove(
        schema_version="0.1.0",
        label=row["label"],
        asset_name=row["asset_name"],
        asset_type=row["asset_type"],
        target_version=row["target_version"],
        previous_version=row["previous_version"],
        content_hash=row["content_hash"],
        reason=row["reason"],
        moved_at=row["moved_at"],
        moved_by=row["moved_by"],
        move_id=row["move_id"],
    ).model_dump(mode="json")
    record["auto"] = False
    return record


def _latest_record(row: sqlite3.Row, asset_name: str) -> dict[str, Any]:
    """A projection record for the auto-maintained ``latest`` label."""
    return {
        "schema_version": "0.1.0",
        "label": RESERVED_LABEL_LATEST,
        "asset_name": asset_name,
        "asset_type": row["asset_type"],
        "target_version": row["version"],
        "previous_version": None,
        "content_hash": _content_hash(row),
        "reason": None,
        "moved_at": row["created_at"],
        "moved_by": "(auto)",
        "move_id": None,
        "auto": True,
    }


def _current_pointer_row(
    conn: sqlite3.Connection, asset_name: str, label: str
) -> sqlite3.Row | None:
    """Projection: the newest history row for ``(asset_name, label)``."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM asset_label_history WHERE asset_name = ? AND label = ? "
        "ORDER BY moved_at DESC, rowid DESC LIMIT 1",
        (asset_name, label),
    ).fetchone()
    return row


def set_label(
    asset_name: str,
    label: str,
    target_version: str,
    *,
    asset_type: str | None = None,
    reason: str | None = None,
    moved_by: str | None = None,
    db_path: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Point ``label`` at ``target_version``, appending an audit row.

    Returns ``(record, moved)``. A no-op set (the label already points at
    ``target_version``) is skipped by default (spec §Edge cases) and returns
    the current record with ``moved=False``. Fail-closed: a non-existent
    target version raises and writes no row; ``latest`` is never settable.
    """
    validate_label_name(label)
    if label == RESERVED_LABEL_LATEST:
        raise ReservedLabelError(
            "'latest' is auto-maintained by the registry and cannot be set."
        )
    conn = _open(db_path)
    try:
        # ADR-0114: a protected label never moves via direct set. Lazy import
        # avoids a module cycle (protected_labels builds on this module).
        from novafabric.registry.protected_labels import active_protection

        protection = active_protection(conn, asset_name, label)
        if protection is not None:
            raise ProtectedLabelError(
                f"Label '{label}' on '{asset_name}' is protected (ADR-0114) — "
                "direct 'nova label set' is refused. Propose the move with "
                f"'nova label propose-move {asset_name} {label} --to <version>' "
                "and have a second principal approve it with "
                "'nova label approve-move'."
            )
        target = _asset_version_row(conn, asset_name, target_version, asset_type)
        if target is None:
            raise LabelTargetNotFoundError(
                f"Asset '{asset_name}' has no version '{target_version}' "
                "in the registry (no label row written)."
            )
        current = _current_pointer_row(conn, asset_name, label)
        if current is not None and current["target_version"] == target_version:
            return _row_to_record(current), False
        move = AssetLabelMove(
            schema_version="0.1.0",
            label=label,
            asset_name=asset_name,
            asset_type=target["asset_type"],
            target_version=target_version,
            previous_version=current["target_version"] if current else None,
            content_hash=_content_hash(target),
            reason=reason,
            moved_at=_now(),
            moved_by=moved_by or getpass.getuser(),
            move_id=new_ulid(),
        )
        conn.execute(
            """
            INSERT INTO asset_label_history
                (move_id, schema_version, label, asset_name, asset_type,
                 target_version, previous_version, content_hash, reason,
                 moved_at, moved_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                move.move_id,
                move.schema_version,
                move.label,
                move.asset_name,
                move.asset_type,
                move.target_version,
                move.previous_version,
                move.content_hash,
                move.reason,
                move.moved_at,
                move.moved_by,
            ),
        )
        conn.commit()
        record = move.model_dump(mode="json")
        record["auto"] = False
        return record, True
    finally:
        conn.close()


def get_label(
    asset_name: str,
    label: str,
    *,
    asset_type: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve ``label`` to its current target version + content hash."""
    validate_label_name(label)
    conn = _open(db_path)
    try:
        if label == RESERVED_LABEL_LATEST:
            row = _latest_version_row(conn, asset_name, asset_type)
            if row is None:
                raise LabelNotFoundError(
                    f"Asset '{asset_name}' has no registered versions, "
                    "so 'latest' does not resolve."
                )
            return _latest_record(row, asset_name)
        current = _current_pointer_row(conn, asset_name, label)
        if current is None:
            raise LabelNotFoundError(
                f"Label '{label}' has never been set on asset '{asset_name}'."
            )
        return _row_to_record(current)
    finally:
        conn.close()


def list_labels(
    asset_name: str,
    *,
    asset_type: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """All labels on an asset: auto ``latest`` first, then explicit labels."""
    conn = _open(db_path)
    try:
        records: list[dict[str, Any]] = []
        latest_row = _latest_version_row(conn, asset_name, asset_type)
        if latest_row is not None:
            records.append(_latest_record(latest_row, asset_name))
        names = [
            r["label"]
            for r in conn.execute(
                "SELECT DISTINCT label FROM asset_label_history "
                "WHERE asset_name = ? ORDER BY label",
                (asset_name,),
            ).fetchall()
        ]
        for name in names:
            row = _current_pointer_row(conn, asset_name, name)
            assert row is not None  # DISTINCT guarantees at least one row
            records.append(_row_to_record(row))
        return records
    finally:
        conn.close()


def label_history(
    asset_name: str,
    label: str | None = None,
    *,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """The append-only move log (newest first), optionally for one label."""
    conn = _open(db_path)
    try:
        query = "SELECT * FROM asset_label_history WHERE asset_name = ?"
        params: list[str] = [asset_name]
        if label is not None:
            validate_label_name(label)
            query += " AND label = ?"
            params.append(label)
        query += " ORDER BY moved_at DESC, rowid DESC"
        return [_row_to_record(row) for row in conn.execute(query, params)]
    finally:
        conn.close()


def parse_asset_ref(ref: str) -> tuple[str, str, str]:
    """Split ``<asset_type>:<asset_name>@<selector>`` into its three parts."""
    head, sep, selector = ref.rpartition("@")
    if not sep or not selector:
        raise UnresolvableRefError(
            f"Invalid asset reference {ref!r}: expected "
            "<asset_type>:<asset_name>@<label-or-version>"
        )
    asset_type, sep, asset_name = head.partition(":")
    if not sep or not asset_type or not asset_name:
        raise UnresolvableRefError(
            f"Invalid asset reference {ref!r}: expected "
            "<asset_type>:<asset_name>@<label-or-version>"
        )
    return asset_type, asset_name, selector


def resolve_asset_ref(
    ref: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve a reference and freeze it into a capsule-ready record.

    The returned dict is a validated
    :class:`~novafabric.spec.asset_labels.ResolvedAssetRef` (ADR-0113 D3):
    the concrete immutable ``resolved_version`` + ``resolved_content_hash``
    the reference named at resolution time. An explicit version that exists
    in the registry takes precedence over a label of the same spelling;
    otherwise the selector is resolved through the label layer
    (fail-closed when neither resolves).
    """
    asset_type, asset_name, selector = parse_asset_ref(ref)
    conn = _open(db_path)
    try:
        resolved_at = _now()
        version_row = _asset_version_row(conn, asset_name, selector, asset_type)
        if version_row is not None:
            content_hash = _content_hash(version_row)
            if content_hash is None:
                raise UnresolvableRefError(
                    f"Asset '{asset_name}@{selector}' has no content_hash; "
                    "the capsule freeze requires a content-addressed asset."
                )
            return ResolvedAssetRef(
                schema_version="0.1.0",
                asset_type=asset_type,
                asset_name=asset_name,
                requested_ref=ref,
                resolved_via="explicit-version",
                resolved_label=None,
                resolved_version=selector,
                resolved_content_hash=content_hash,
                resolved_at=resolved_at,
            ).model_dump(mode="json")
    finally:
        conn.close()

    try:
        pointer = get_label(
            asset_name, selector, asset_type=asset_type, db_path=db_path
        )
    except (LabelNotFoundError, ValueError) as exc:
        raise UnresolvableRefError(
            f"Reference {ref!r} does not resolve: '{selector}' is neither an "
            f"existing version nor a set label of '{asset_type}:{asset_name}'."
        ) from exc
    if pointer["content_hash"] is None:
        raise UnresolvableRefError(
            f"Label '{selector}' on '{asset_name}' points at version "
            f"'{pointer['target_version']}' which has no content_hash; "
            "the capsule freeze requires a content-addressed asset."
        )
    return ResolvedAssetRef(
        schema_version="0.1.0",
        asset_type=asset_type,
        asset_name=asset_name,
        requested_ref=ref,
        resolved_via="label",
        resolved_label=selector,
        resolved_version=pointer["target_version"],
        resolved_content_hash=pointer["content_hash"],
        resolved_at=_now(),
        label_move_id=pointer["move_id"],
        label_moved_at=pointer["moved_at"],
    ).model_dump(mode="json")
