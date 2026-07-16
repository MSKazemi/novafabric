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

"""Protected-label maker-checker registry operations (ADR-0114).

Extends the ADR-0113 deployment-label layer with three **additive** tables:

* ``asset_label_protection`` — append-only protect/unprotect events; the
  current setting is a projection (newest row per ``(asset_name, label)``).
* ``asset_label_pending_moves`` — one row per in-flight protected move
  (state machine ``pending → applied | rejected | expired``).
* ``asset_label_move_approvals`` — append-only checker decisions, each
  Ed25519-signed via the ADR-0058 keyring.

Invariants (ADR-0114 D1–D3; spec ``design/spec/protected-labels-v0.md``):

* **Two-principal rule, crypto-level** — an approval is refused when the
  approver's key fingerprint equals the proposer's key fingerprint *or* the
  identities match (mirrors :func:`novafabric.registry.service.approve_promotion`).
  A duplicate approver is recorded but counted once.
* **Policy gate** — ``pending → applied`` additionally requires the
  snapshotted ``policy_ref`` Rego policy (ADR-0019) to allow; ``policy_ref``
  absent means the built-in default policy (exactly the distinctness
  invariants, enforced in code — local-first, no OPA needed).
* **Atomic apply** — the ``asset_label_history`` audit row (ADR-0113,
  sharing the pending move's ULID as ``move_id``) and the state transition
  commit in one SQLite transaction; a protected label is never half-moved.
* **Free labels unchanged** — no protection record means
  :func:`novafabric.registry.labels.set_label` behaves exactly as ADR-0113.
"""

from __future__ import annotations

import getpass
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from novafabric.capture._ulid import new_ulid
from novafabric.registry import labels as _labels
from novafabric.registry.labels import (
    LabelError,
    LabelTargetNotFoundError,
    ReservedLabelError,
)
from novafabric.spec.asset_labels import (
    RESERVED_LABEL_LATEST,
    validate_label_name,
)
from novafabric.spec.protected_labels import (
    TERMINAL_MOVE_STATES,
    LabelProtectionConfig,
    PendingLabelMove,
)
from novafabric.trust.keyring import ensure_keypair, sign_payload


class NotProtectedError(LabelError):
    """``propose-move`` on a free label — use ``nova label set`` instead."""


class PendingMoveExistsError(LabelError):
    """A non-terminal pending move already exists for this (asset, label)."""


class PendingMoveNotFoundError(LabelError):
    """No pending move matches the requested ``move_id``."""


class MoveStateError(LabelError):
    """The move is in a terminal state (``applied``/``rejected``/``expired``)."""


class SelfApprovalError(LabelError):
    """SoD violation: the checker matches the maker (identity or keypair)."""


_PROTECTED_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS asset_label_protection (
    event_id           TEXT PRIMARY KEY,
    schema_version     TEXT NOT NULL,
    asset_name         TEXT NOT NULL,
    label              TEXT NOT NULL,
    protected          INTEGER NOT NULL,
    required_approvals INTEGER NOT NULL,
    policy_ref         TEXT,
    note               TEXT,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_label_protection_key
    ON asset_label_protection(asset_name, label, created_at DESC);
CREATE TRIGGER IF NOT EXISTS trg_asset_label_protection_no_update
    BEFORE UPDATE ON asset_label_protection
    BEGIN SELECT RAISE(ABORT, 'asset_label_protection is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_asset_label_protection_no_delete
    BEFORE DELETE ON asset_label_protection
    BEGIN SELECT RAISE(ABORT, 'asset_label_protection is append-only'); END;

CREATE TABLE IF NOT EXISTS asset_label_pending_moves (
    move_id            TEXT PRIMARY KEY,
    schema_version     TEXT NOT NULL,
    asset_name         TEXT NOT NULL,
    label              TEXT NOT NULL,
    from_version       TEXT,
    proposed_version   TEXT NOT NULL,
    proposed_by        TEXT NOT NULL,
    proposer_key_fp    TEXT NOT NULL,
    proposer_sig       TEXT NOT NULL,
    proposed_at        TEXT NOT NULL,
    reason             TEXT,
    policy_ref         TEXT,
    required_approvals INTEGER NOT NULL,
    state              TEXT NOT NULL,
    expires_at         TEXT,
    evidence_ref       TEXT,
    applied_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_label_pending_moves_key
    ON asset_label_pending_moves(asset_name, label, state);
CREATE TRIGGER IF NOT EXISTS trg_asset_label_pending_moves_no_delete
    BEFORE DELETE ON asset_label_pending_moves
    BEGIN SELECT RAISE(ABORT, 'asset_label_pending_moves rows are never deleted'); END;

CREATE TABLE IF NOT EXISTS asset_label_move_approvals (
    approval_id     TEXT PRIMARY KEY,
    move_id         TEXT NOT NULL,
    approver        TEXT NOT NULL,
    approver_key_fp TEXT NOT NULL,
    approver_sig    TEXT NOT NULL,
    decision        TEXT NOT NULL,
    note            TEXT,
    approved_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_label_move_approvals_move
    ON asset_label_move_approvals(move_id, approved_at);
CREATE TRIGGER IF NOT EXISTS trg_asset_label_move_approvals_no_update
    BEFORE UPDATE ON asset_label_move_approvals
    BEGIN SELECT RAISE(ABORT, 'asset_label_move_approvals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_asset_label_move_approvals_no_delete
    BEFORE DELETE ON asset_label_move_approvals
    BEGIN SELECT RAISE(ABORT, 'asset_label_move_approvals is append-only'); END;
"""


def ensure_protected_schema(conn: sqlite3.Connection) -> None:
    """Create the additive protected-label tables (idempotent, additive-only)."""
    conn.executescript(_PROTECTED_SCHEMA_SQL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open(db_path: Path | None) -> sqlite3.Connection:
    conn = _labels._open(db_path)
    ensure_protected_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Signature pre-images (deterministic, so signatures stay verifiable)
# ---------------------------------------------------------------------------


def proposal_payload(
    move_id: str,
    asset_name: str,
    label: str,
    from_version: str | None,
    proposed_version: str,
    proposed_at: str,
) -> bytes:
    """The canonical bytes the maker signs when proposing a move."""
    return (
        f"label-move|{move_id}|{asset_name}:{label}|"
        f"{from_version or ''}->{proposed_version}|{proposed_at}"
    ).encode()


def approval_payload(
    move_id: str, decision: str, approver: str, approved_at: str
) -> bytes:
    """The canonical bytes a checker signs when approving/rejecting."""
    return f"label-move-approval|{move_id}|{decision}|{approver}|{approved_at}".encode()


# ---------------------------------------------------------------------------
# Record builders (schema-shaped, validated through the pydantic models)
# ---------------------------------------------------------------------------


def _config_record(row: sqlite3.Row) -> dict[str, Any]:
    return LabelProtectionConfig(
        schema_version="0.1.0",
        asset_name=row["asset_name"],
        label=row["label"],
        protected=bool(row["protected"]),
        required_approvals=row["required_approvals"],
        policy_ref=row["policy_ref"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        note=row["note"],
    ).model_dump(mode="json", exclude_none=True)


def _move_record(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    approvals = [
        {
            "approver": a["approver"],
            "approved_at": a["approved_at"],
            "decision": a["decision"],
            "approver_key_fp": a["approver_key_fp"],
            "approver_signature": a["approver_sig"],
            "note": a["note"],
        }
        for a in conn.execute(
            "SELECT * FROM asset_label_move_approvals WHERE move_id = ? "
            "ORDER BY approved_at, rowid",
            (row["move_id"],),
        )
    ]
    record = PendingLabelMove(
        schema_version="0.1.0",
        move_id=row["move_id"],
        asset_name=row["asset_name"],
        label=row["label"],
        from_version=row["from_version"],
        proposed_version=row["proposed_version"],
        proposed_by=row["proposed_by"],
        proposer_key_fp=row["proposer_key_fp"],
        proposer_signature=row["proposer_sig"],
        proposed_at=row["proposed_at"],
        state=row["state"],
        approvals=approvals,  # type: ignore[arg-type]
        reason=row["reason"],
        policy_ref=row["policy_ref"],
        required_approvals=row["required_approvals"],
        expires_at=row["expires_at"],
        evidence_ref=row["evidence_ref"],
        applied_at=row["applied_at"],
    ).model_dump(mode="json", exclude_none=True)
    # from_version is required-but-nullable in the schema — keep it present.
    record.setdefault("from_version", None)
    return record


# ---------------------------------------------------------------------------
# Protection configuration (ADR-0114 D1)
# ---------------------------------------------------------------------------


def _protection_row(
    conn: sqlite3.Connection, asset_name: str, label: str
) -> sqlite3.Row | None:
    """Projection: the newest protection event for ``(asset_name, label)``."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM asset_label_protection WHERE asset_name = ? AND label = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (asset_name, label),
    ).fetchone()
    return row


def active_protection(
    conn: sqlite3.Connection, asset_name: str, label: str
) -> dict[str, Any] | None:
    """The active protection config, or ``None`` if the label is free.

    Called by :func:`novafabric.registry.labels.set_label` (lazy import) to
    refuse direct sets on protected labels; ensures its own schema so a
    pre-0114 database answers "free" instead of erroring.
    """
    ensure_protected_schema(conn)
    row = _protection_row(conn, asset_name, label)
    if row is None or not row["protected"]:
        return None
    return _config_record(row)


def protect_label(
    asset_name: str,
    label: str,
    *,
    protected: bool = True,
    required_approvals: int = 1,
    policy_ref: str | None = None,
    note: str | None = None,
    created_by: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Mark a label protected (or free again), appending a config event row.

    Protecting an already-assigned label does not move it — it only governs
    future moves. ``protected=False`` reverts to free ADR-0113 behaviour;
    any in-flight pending move keeps resolving under its propose-time
    snapshot (spec §Edge cases).
    """
    validate_label_name(label)
    if label == RESERVED_LABEL_LATEST:
        raise ReservedLabelError(
            "'latest' is auto-maintained by the registry and cannot be protected."
        )
    if required_approvals < 1:
        raise ValueError("required_approvals must be >= 1")
    conn = _open(db_path)
    try:
        conn.execute(
            """
            INSERT INTO asset_label_protection
                (event_id, schema_version, asset_name, label, protected,
                 required_approvals, policy_ref, note, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_ulid(),
                "0.1.0",
                asset_name,
                label,
                1 if protected else 0,
                required_approvals,
                policy_ref,
                note,
                created_by or getpass.getuser(),
                _now(),
            ),
        )
        conn.commit()
        row = _protection_row(conn, asset_name, label)
        assert row is not None  # just inserted
        return _config_record(row)
    finally:
        conn.close()


def get_protection(
    asset_name: str,
    label: str,
    *,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """The current protection config record, or ``None`` if never configured."""
    validate_label_name(label)
    conn = _open(db_path)
    try:
        row = _protection_row(conn, asset_name, label)
        return None if row is None else _config_record(row)
    finally:
        conn.close()


def list_protections(
    asset_name: str, *, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """Current protection config per label (projection of newest events)."""
    conn = _open(db_path)
    try:
        names = [
            r["label"]
            for r in conn.execute(
                "SELECT DISTINCT label FROM asset_label_protection "
                "WHERE asset_name = ? ORDER BY label",
                (asset_name,),
            )
        ]
        records = []
        for name in names:
            row = _protection_row(conn, asset_name, name)
            assert row is not None  # DISTINCT guarantees at least one row
            records.append(_config_record(row))
        return records
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Maker step (ADR-0114 D2.1)
# ---------------------------------------------------------------------------


def propose_move(
    asset_name: str,
    label: str,
    proposed_version: str,
    *,
    asset_type: str | None = None,
    reason: str | None = None,
    identity: str | None = None,
    expires_at: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Propose a protected-label move (maker step) — the label does NOT move.

    Fail-closed: the target version must exist; the label must actually be
    protected (free labels use :func:`~novafabric.registry.labels.set_label`);
    at most one non-terminal pending move per ``(asset_name, label)``. The
    proposal is Ed25519-signed with the maker's keyring key (ADR-0058).
    """
    validate_label_name(label)
    if label == RESERVED_LABEL_LATEST:
        raise ReservedLabelError(
            "'latest' is auto-maintained by the registry and cannot be moved."
        )
    conn = _open(db_path)
    try:
        config_row = _protection_row(conn, asset_name, label)
        if config_row is None or not config_row["protected"]:
            raise NotProtectedError(
                f"Label '{label}' on '{asset_name}' is not protected — "
                "free labels move directly with 'nova label set' (ADR-0113)."
            )
        target = _labels._asset_version_row(
            conn, asset_name, proposed_version, asset_type
        )
        if target is None:
            raise LabelTargetNotFoundError(
                f"Asset '{asset_name}' has no version '{proposed_version}' "
                "in the registry (no move proposed)."
            )
        _expire_stale_moves(conn, asset_name, label)
        existing = conn.execute(
            "SELECT move_id FROM asset_label_pending_moves "
            "WHERE asset_name = ? AND label = ? AND state = 'pending'",
            (asset_name, label),
        ).fetchone()
        if existing is not None:
            raise PendingMoveExistsError(
                f"A pending move ({existing['move_id']}) already exists for "
                f"label '{label}' on '{asset_name}'. Approve, reject, or let "
                "it expire before proposing another."
            )
        current = _labels._current_pointer_row(conn, asset_name, label)
        from_version = current["target_version"] if current else None
        if from_version == proposed_version:
            raise LabelError(
                f"'{label}' already points at {proposed_version} — "
                "nothing to propose."
            )

        proposer = identity or getpass.getuser()
        private_key, key_fp = ensure_keypair(proposer)
        move_id = new_ulid()
        proposed_at = _now()
        sig = sign_payload(
            private_key,
            proposal_payload(
                move_id, asset_name, label, from_version, proposed_version, proposed_at
            ),
        )
        conn.execute(
            """
            INSERT INTO asset_label_pending_moves
                (move_id, schema_version, asset_name, label, from_version,
                 proposed_version, proposed_by, proposer_key_fp, proposer_sig,
                 proposed_at, reason, policy_ref, required_approvals, state,
                 expires_at, evidence_ref, applied_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)
            """,
            (
                move_id,
                "0.1.0",
                asset_name,
                label,
                from_version,
                proposed_version,
                proposer,
                key_fp,
                sig,
                proposed_at,
                reason,
                config_row["policy_ref"],
                config_row["required_approvals"],
                expires_at,
            ),
        )
        conn.commit()
        row = _get_move_row(conn, asset_name, move_id)
        assert row is not None  # just inserted
        return _move_record(conn, row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Checker step + atomic apply (ADR-0114 D2.2–D2.3, D3)
# ---------------------------------------------------------------------------


def _get_move_row(
    conn: sqlite3.Connection, asset_name: str, move_id: str
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM asset_label_pending_moves WHERE asset_name = ? AND move_id = ?",
        (asset_name, move_id),
    ).fetchone()
    return row


def _expire_stale_moves(
    conn: sqlite3.Connection, asset_name: str, label: str
) -> None:
    """Lazy transition: past ``expires_at`` a pending move becomes expired."""
    conn.execute(
        "UPDATE asset_label_pending_moves SET state = 'expired' "
        "WHERE asset_name = ? AND label = ? AND state = 'pending' "
        "AND expires_at IS NOT NULL AND expires_at < ?",
        (asset_name, label, _now()),
    )


def _policy_gate(record: dict[str, Any], approver: str) -> tuple[bool, str]:
    """Evaluate the snapshotted Rego policy (ADR-0019) for a ready move.

    ``policy_ref`` absent → the built-in default protected-label policy,
    which is exactly the ADR-0003 distinctness invariants — already enforced
    in code before this gate runs, so the default allows. A named policy that
    cannot be read fails closed.
    """
    policy_ref = record.get("policy_ref")
    if not policy_ref:
        return True, "built-in default policy (distinct-approver invariants)"
    path = Path(policy_ref)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"policy_ref '{policy_ref}' unreadable (fail-closed): {exc}"

    from novafabric.policy import (  # noqa: PLC0415 — lazy: only custom policies
        OpaEngine,
        PolicyInput,
        PolicyResource,
        PolicySubject,
        get_policy_engine,
    )

    input_ = PolicyInput(
        action="label.protected_move",
        subject=PolicySubject(user=approver),
        resource=PolicyResource(
            kind="asset",
            ref=f"{record['asset_name']}@{record['proposed_version']}",
        ),
        context={"pending_move": record},
    )
    engine = get_policy_engine()
    if isinstance(engine, OpaEngine):
        decision = engine.evaluate(input_, policy_source=source)
    else:
        # NoopEngine — get_policy_engine already warned that gates are disabled.
        decision = engine.evaluate(input_)
    return decision.allow, decision.reason or (
        "allowed" if decision.allow else "denied"
    )


def approve_move(
    asset_name: str,
    label: str,
    move_id: str,
    *,
    identity: str | None = None,
    reject: bool = False,
    note: str | None = None,
    db_path: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Record a checker decision; apply the move when the gate is satisfied.

    Returns ``(record, detail)`` where ``detail`` is a human-readable outcome
    ("applied", "rejected", "pending (…)"). SoD is enforced at the crypto
    level: the checker's Ed25519 key fingerprint and identity must both
    differ from the maker's (mirrors ADR-0058 ``approve_promotion``). A
    duplicate approver is recorded but counts once. Apply is atomic: the
    ADR-0113 ``asset_label_history`` row (reusing this move's ULID) and the
    state transition commit together.
    """
    validate_label_name(label)
    conn = _open(db_path)
    try:
        row = _get_move_row(conn, asset_name, move_id)
        if row is None or row["label"] != label:
            raise PendingMoveNotFoundError(
                f"No move '{move_id}' found for label '{label}' on '{asset_name}'."
            )
        _expire_stale_moves(conn, asset_name, label)
        conn.commit()
        row = _get_move_row(conn, asset_name, move_id)
        assert row is not None  # no_delete trigger guarantees the row remains
        if row["state"] in TERMINAL_MOVE_STATES:
            raise MoveStateError(
                f"Move '{move_id}' is '{row['state']}' (terminal) — "
                "propose a new move instead."
            )

        approver = identity or getpass.getuser()
        private_key, key_fp = ensure_keypair(approver)
        if key_fp == row["proposer_key_fp"]:
            raise SelfApprovalError(
                "Approver key fingerprint matches proposer key fingerprint — "
                "SoD violation: maker and checker must use distinct keypairs "
                "(ADR-0114 reusing ADR-0003/0058)."
            )
        if approver == row["proposed_by"]:
            raise SelfApprovalError(
                f"Approver identity '{approver}' matches proposer — "
                "SoD violation: maker and checker must be different identities."
            )

        decision = "reject" if reject else "approve"
        approved_at = _now()
        sig = sign_payload(
            private_key, approval_payload(move_id, decision, approver, approved_at)
        )
        conn.execute(
            """
            INSERT INTO asset_label_move_approvals
                (approval_id, move_id, approver, approver_key_fp, approver_sig,
                 decision, note, approved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_ulid(), move_id, approver, key_fp, sig, decision, note, approved_at),
        )

        if reject:
            conn.execute(
                "UPDATE asset_label_pending_moves SET state = 'rejected' "
                "WHERE move_id = ?",
                (move_id,),
            )
            conn.commit()
            return _refreshed(conn, asset_name, move_id), "rejected"

        distinct = {
            a["approver_key_fp"]
            for a in conn.execute(
                "SELECT approver_key_fp FROM asset_label_move_approvals "
                "WHERE move_id = ? AND decision = 'approve'",
                (move_id,),
            )
        }
        required: int = row["required_approvals"]
        if len(distinct) < required:
            conn.commit()
            detail = f"pending ({len(distinct)}/{required} distinct approvals)"
            return _refreshed(conn, asset_name, move_id), detail

        allow, reason = _policy_gate(_move_record(conn, row), approver)
        if not allow:
            conn.commit()  # the approval itself is still recorded
            return _refreshed(conn, asset_name, move_id), (
                f"pending (policy denied: {reason})"
            )

        # ---- atomic apply: history row + state transition, one commit ----
        applied_at = _now()
        current = _labels._current_pointer_row(conn, asset_name, label)
        previous = current["target_version"] if current else None
        if previous != row["proposed_version"]:
            target = _labels._asset_version_row(
                conn, asset_name, row["proposed_version"]
            )
            if target is None:
                raise LabelTargetNotFoundError(
                    f"Version '{row['proposed_version']}' no longer exists in "
                    "the registry — move not applied."
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
                    move_id,
                    "0.1.0",
                    label,
                    asset_name,
                    target["asset_type"],
                    row["proposed_version"],
                    previous,
                    _labels._content_hash(target),
                    row["reason"],
                    applied_at,
                    row["proposed_by"],
                ),
            )
        conn.execute(
            "UPDATE asset_label_pending_moves "
            "SET state = 'applied', applied_at = ? WHERE move_id = ?",
            (applied_at, move_id),
        )
        conn.commit()
        return _refreshed(conn, asset_name, move_id), "applied"
    finally:
        conn.close()


def _refreshed(
    conn: sqlite3.Connection, asset_name: str, move_id: str
) -> dict[str, Any]:
    row = _get_move_row(conn, asset_name, move_id)
    assert row is not None  # no_delete trigger guarantees the row remains
    return _move_record(conn, row)


# ---------------------------------------------------------------------------
# Status / listing
# ---------------------------------------------------------------------------


def get_move(
    asset_name: str, move_id: str, *, db_path: Path | None = None
) -> dict[str, Any]:
    """One pending-move record by id."""
    conn = _open(db_path)
    try:
        row = _get_move_row(conn, asset_name, move_id)
        if row is None:
            raise PendingMoveNotFoundError(
                f"No move '{move_id}' found on '{asset_name}'."
            )
        return _move_record(conn, row)
    finally:
        conn.close()


def list_moves(
    asset_name: str,
    label: str | None = None,
    *,
    state: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Pending-move records for an asset (newest first), optionally filtered."""
    conn = _open(db_path)
    try:
        query = "SELECT * FROM asset_label_pending_moves WHERE asset_name = ?"
        params: list[str] = [asset_name]
        if label is not None:
            validate_label_name(label)
            query += " AND label = ?"
            params.append(label)
        if state is not None:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY proposed_at DESC, rowid DESC"
        return [
            _move_record(conn, row) for row in conn.execute(query, params).fetchall()
        ]
    finally:
        conn.close()


def label_status(
    asset_name: str,
    label: str | None = None,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Protection config, current pointers, and pending moves for an asset."""
    protections = list_protections(asset_name, db_path=db_path)
    pointers = _labels.list_labels(asset_name, db_path=db_path)
    moves = list_moves(asset_name, label, db_path=db_path)
    if label is not None:
        protections = [p for p in protections if p["label"] == label]
        pointers = [p for p in pointers if p["label"] == label]
    return {
        "asset_name": asset_name,
        "protections": protections,
        "pointers": pointers,
        "pending_moves": moves,
    }
