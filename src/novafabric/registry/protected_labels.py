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

Invariants (ADR-0114 D1–D3; spec ``the private design/spec/protected-labels-v0.md``):

* **Two-principal rule, bound to the signer trust list** — the maker and every
  checker sign with an **existing** keyring key (never minted on this path) that
  the ADR-0312 signer trust list (``NOVAFABRIC_PROMOTE_SIGNERS`` →
  ``$NOVAFABRIC_HOME/promote-signers.yaml``) binds to the claimed identity, with
  the ``proposer`` role for the maker and ``approver`` for a checker. An approval
  is refused when the checker's **bound** identity or key fingerprint equals the
  maker's (mirrors :func:`novafabric.registry.service.approve_promotion`). The
  stored maker signature is re-verified at approve time, and only approvals that
  verify against the trust list count — once per bound identity.
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
import hashlib
import json
import logging
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
from novafabric.registry.signers import (
    SignerTrustList,
    SignerVerificationError,
    TrustedSigner,
    load_signer_trust_list,
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

logger = logging.getLogger(__name__)


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


class ProtectionRelaxationRequiresApprovalError(LabelError):
    """Removing or weakening protection on a protected label needs maker-checker.

    Unprotecting, lowering ``required_approvals`` or replacing an existing
    ``policy_ref`` would let one person switch the two-person rule off, move the
    label, and switch it back on. Use :func:`propose_protection_change`.
    """


class PendingProtectionChangeExistsError(LabelError):
    """A pending protection change already exists for this (asset, label)."""


class ProtectionChangeNotFoundError(LabelError):
    """No protection change matches the requested ``change_id``."""


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

-- Maker-checker for relaxing a protection (unprotect, fewer approvals, another
-- policy). base_* snapshot the protection in force when the change was proposed:
-- its event id (a change applies only on top of exactly that config) and its
-- required_approvals (the quorum the relaxation needs).
CREATE TABLE IF NOT EXISTS asset_label_protection_changes (
    change_id               TEXT PRIMARY KEY,
    schema_version          TEXT NOT NULL,
    asset_name              TEXT NOT NULL,
    label                   TEXT NOT NULL,
    base_event_id           TEXT NOT NULL,
    base_required_approvals INTEGER NOT NULL,
    protected               INTEGER NOT NULL,
    required_approvals      INTEGER NOT NULL,
    policy_ref              TEXT,
    note                    TEXT,
    proposed_by             TEXT NOT NULL,
    proposer_key_fp         TEXT NOT NULL,
    proposer_sig            TEXT NOT NULL,
    proposed_at             TEXT NOT NULL,
    state                   TEXT NOT NULL,
    applied_at              TEXT,
    applied_event_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_label_protection_changes_key
    ON asset_label_protection_changes(asset_name, label, state);
CREATE TRIGGER IF NOT EXISTS trg_asset_label_protection_changes_no_delete
    BEFORE DELETE ON asset_label_protection_changes
    BEGIN SELECT RAISE(ABORT, 'asset_label_protection_changes rows are never deleted'); END;

CREATE TABLE IF NOT EXISTS asset_label_protection_change_approvals (
    approval_id     TEXT PRIMARY KEY,
    change_id       TEXT NOT NULL,
    approver        TEXT NOT NULL,
    approver_key_fp TEXT NOT NULL,
    approver_sig    TEXT NOT NULL,
    decision        TEXT NOT NULL,
    note            TEXT,
    approved_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asset_label_protection_change_approvals_change
    ON asset_label_protection_change_approvals(change_id, approved_at);
CREATE TRIGGER IF NOT EXISTS trg_asset_label_protection_change_approvals_no_update
    BEFORE UPDATE ON asset_label_protection_change_approvals
    BEGIN SELECT RAISE(ABORT, 'asset_label_protection_change_approvals is append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_asset_label_protection_change_approvals_no_delete
    BEFORE DELETE ON asset_label_protection_change_approvals
    BEGIN SELECT RAISE(ABORT, 'asset_label_protection_change_approvals is append-only'); END;
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
    """Protect a label, or raise its protection, appending a config event row.

    Protecting an already-assigned label does not move it — it only governs
    future moves. Protecting a free label, raising ``required_approvals`` or
    adding a ``policy_ref`` is a single-actor action.

    Relaxing an **active** protection — ``protected=False``, fewer
    ``required_approvals``, or a different or removed ``policy_ref`` — is refused
    here: it needs the maker-checker of :func:`propose_protection_change` +
    :func:`approve_protection_change`. Any in-flight pending move keeps resolving
    under its propose-time snapshot (spec §Edge cases).

    Raises:
        ProtectionRelaxationRequiresApprovalError: the change would relax an
            active protection.
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
        current = _protection_row(conn, asset_name, label)
        if is_protection_relaxation(
            current,
            protected=protected,
            required_approvals=required_approvals,
            policy_ref=policy_ref,
        ):
            assert current is not None  # a relaxation needs an active protection
            raise ProtectionRelaxationRequiresApprovalError(
                f"'{label}' on '{asset_name}' is protected "
                f"(required approvals: {current['required_approvals']}"
                + (f", policy: {current['policy_ref']}" if current["policy_ref"] else "")
                + "). Removing or weakening that protection needs the same maker-checker "
                "as a move (ADR-0114): propose it with propose_protection_change "
                "(`nova label protect ... --identity <maker>`) and have distinct "
                "checkers approve it (`nova label approve-move`)."
            )
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
# Relaxing a protection needs maker-checker (ADR-0114 Amendment 2)
# ---------------------------------------------------------------------------

PROTECTION_CHANGE_STATES = ("pending", "applied", "rejected", "superseded")


def is_protection_relaxation(
    current: sqlite3.Row | dict[str, Any] | None,
    *,
    protected: bool,
    required_approvals: int,
    policy_ref: str | None,
) -> bool:
    """True when the requested config is weaker than the active protection.

    Weaker means: protection removed, fewer required approvals, or an existing
    ``policy_ref`` replaced or removed (a different policy cannot be ranked, so it
    is treated as weaker). No active protection → nothing to relax.
    """
    if current is None or not current["protected"]:
        return False
    if not protected:
        return True
    if required_approvals < int(current["required_approvals"]):
        return True
    current_policy = current["policy_ref"]
    return bool(current_policy) and policy_ref != current_policy


def _canonical(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def protection_change_payload(
    change_id: str,
    asset_name: str,
    label: str,
    base_event_id: str,
    protected: bool,
    required_approvals: int,
    policy_ref: str | None,
    proposed_at: str,
) -> bytes:
    """The canonical bytes the maker signs when proposing a protection change."""
    return _canonical(
        {
            "scheme": "nova-label-protection-change/v1",
            "step": "propose",
            "change_id": change_id,
            "asset": asset_name,
            "label": label,
            "base_event_id": base_event_id,
            "protected": bool(protected),
            "required_approvals": int(required_approvals),
            "policy_ref": policy_ref,
            "proposed_at": proposed_at,
        }
    )


def protection_approval_payload(
    change_id: str, decision: str, approver: str, approved_at: str, proposer_sig: str
) -> bytes:
    """The canonical bytes a checker signs; bound to the exact maker signature."""
    return _canonical(
        {
            "scheme": "nova-label-protection-change/v1",
            "step": decision,
            "change_id": change_id,
            "approver": approver,
            "approved_at": approved_at,
            "proposer_sig_sha256": hashlib.sha256(proposer_sig.encode()).hexdigest(),
        }
    )


def _change_record(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
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
            "SELECT * FROM asset_label_protection_change_approvals WHERE change_id = ? "
            "ORDER BY approved_at, rowid",
            (row["change_id"],),
        )
    ]
    return {
        "change_id": row["change_id"],
        "asset_name": row["asset_name"],
        "label": row["label"],
        "base_event_id": row["base_event_id"],
        "base_required_approvals": row["base_required_approvals"],
        "protected": bool(row["protected"]),
        "required_approvals": row["required_approvals"],
        "policy_ref": row["policy_ref"],
        "note": row["note"],
        "proposed_by": row["proposed_by"],
        "proposer_key_fp": row["proposer_key_fp"],
        "proposer_signature": row["proposer_sig"],
        "proposed_at": row["proposed_at"],
        "state": row["state"],
        "applied_at": row["applied_at"],
        "applied_event_id": row["applied_event_id"],
        "approvals": approvals,
    }


def _get_change_row(
    conn: sqlite3.Connection, asset_name: str, change_id: str
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM asset_label_protection_changes WHERE asset_name = ? AND change_id = ?",
        (asset_name, change_id),
    ).fetchone()
    return row


def propose_protection_change(
    asset_name: str,
    label: str,
    *,
    protected: bool = True,
    required_approvals: int = 1,
    policy_ref: str | None = None,
    note: str | None = None,
    identity: str | None = None,
    db_path: Path | None = None,
    trust_list: SignerTrustList | None = None,
) -> dict[str, Any]:
    """Propose relaxing an active protection (maker step) — nothing changes yet.

    The requested config must actually be a relaxation (see
    :func:`is_protection_relaxation`); anything else goes through
    :func:`protect_label`. The maker signs with an **existing** keyring key the
    ADR-0312 signer trust list binds to *identity* with the ``proposer`` role. The
    change records the protection in force (its event id and required approvals):
    it needs that many distinct trust-listed checkers, and it applies only on top
    of exactly that config. At most one pending change per ``(asset, label)``.

    Raises:
        NotProtectedError: the label is not protected (nothing to relax).
        LabelError: the requested config is not a relaxation.
        PendingProtectionChangeExistsError: a change is already pending.
        KeyNotFoundError / SignerVerificationError: as :func:`propose_move`.
    """
    validate_label_name(label)
    if required_approvals < 1:
        raise ValueError("required_approvals must be >= 1")
    trust = trust_list or load_signer_trust_list()
    conn = _open(db_path)
    try:
        current = _protection_row(conn, asset_name, label)
        if current is None or not current["protected"]:
            raise NotProtectedError(
                f"Label '{label}' on '{asset_name}' is not protected — there is no "
                "protection to relax."
            )
        if not is_protection_relaxation(
            current,
            protected=protected,
            required_approvals=required_approvals,
            policy_ref=policy_ref,
        ):
            raise LabelError(
                "The requested protection is not a relaxation of the active one; "
                "apply it directly with protect_label / `nova label protect`."
            )
        existing = conn.execute(
            "SELECT change_id FROM asset_label_protection_changes "
            "WHERE asset_name = ? AND label = ? AND state = 'pending'",
            (asset_name, label),
        ).fetchone()
        if existing is not None:
            raise PendingProtectionChangeExistsError(
                f"A pending protection change ({existing['change_id']}) already exists "
                f"for label '{label}' on '{asset_name}'. Approve or reject it first."
            )

        proposer = identity or getpass.getuser()
        private_key, key_fp = ensure_keypair(proposer, create=False)
        change_id = new_ulid()
        proposed_at = _now()
        payload = protection_change_payload(
            change_id,
            asset_name,
            label,
            str(current["event_id"]),
            protected,
            required_approvals,
            policy_ref,
            proposed_at,
        )
        sig = sign_payload(private_key, payload)
        maker = trust.verify(
            claimed_identity=proposer,
            key_fp=key_fp,
            role="proposer",
            payload=payload,
            signature=sig,
            party="proposer",
        )
        conn.execute(
            """
            INSERT INTO asset_label_protection_changes
                (change_id, schema_version, asset_name, label, base_event_id,
                 base_required_approvals, protected, required_approvals, policy_ref,
                 note, proposed_by, proposer_key_fp, proposer_sig, proposed_at, state,
                 applied_at, applied_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL)
            """,
            (
                change_id,
                "0.1.0",
                asset_name,
                label,
                current["event_id"],
                current["required_approvals"],
                1 if protected else 0,
                required_approvals,
                policy_ref,
                note,
                maker.identity,
                maker.fingerprint,
                sig,
                proposed_at,
            ),
        )
        conn.commit()
        row = _get_change_row(conn, asset_name, change_id)
        assert row is not None  # just inserted
        return _change_record(conn, row)
    finally:
        conn.close()


def _verify_change_maker(trust: SignerTrustList, row: sqlite3.Row) -> TrustedSigner:
    """Re-verify the stored maker signature over the requested config."""
    return trust.verify(
        claimed_identity=str(row["proposed_by"]),
        key_fp=str(row["proposer_key_fp"]),
        role="proposer",
        payload=protection_change_payload(
            row["change_id"],
            row["asset_name"],
            row["label"],
            row["base_event_id"],
            bool(row["protected"]),
            int(row["required_approvals"]),
            row["policy_ref"],
            row["proposed_at"],
        ),
        signature=str(row["proposer_sig"]),
        party="proposer",
    )


def _verified_change_approvers(
    conn: sqlite3.Connection, trust: SignerTrustList, row: sqlite3.Row, maker: TrustedSigner
) -> set[str]:
    """Bound identities of stored ``approve`` decisions that verify today (not the maker)."""
    identities: set[str] = set()
    for a in conn.execute(
        "SELECT approver, approver_key_fp, approver_sig, approved_at "
        "FROM asset_label_protection_change_approvals "
        "WHERE change_id = ? AND decision = 'approve'",
        (row["change_id"],),
    ):
        try:
            signer = trust.verify(
                claimed_identity=str(a["approver"]),
                key_fp=str(a["approver_key_fp"]),
                role="approver",
                payload=protection_approval_payload(
                    row["change_id"],
                    "approve",
                    str(a["approver"]),
                    str(a["approved_at"]),
                    str(row["proposer_sig"]),
                ),
                signature=str(a["approver_sig"]),
                party="approver",
            )
        except SignerVerificationError as exc:
            logger.warning(
                "protection change %s: stored approval by %r not counted: %s",
                row["change_id"], a["approver"], exc,
            )
            continue
        if signer.identity == maker.identity or signer.fingerprint == maker.fingerprint:
            continue
        identities.add(signer.identity)
    return identities


def approve_protection_change(
    asset_name: str,
    label: str,
    change_id: str,
    *,
    identity: str | None = None,
    reject: bool = False,
    note: str | None = None,
    db_path: Path | None = None,
    trust_list: SignerTrustList | None = None,
) -> tuple[dict[str, Any], str]:
    """Record a checker decision on a protection change; apply it at quorum.

    Same rules as :func:`approve_move`: the checker signs with an existing,
    trust-listed key (``approver`` role); the maker's stored signature is
    re-verified (an edited row is refused); separation of duties compares bound
    identities and fingerprints; only verifying approvals count, once per bound
    identity. The quorum is the ``required_approvals`` of the protection that was
    in force when the change was proposed.

    The change applies only on top of exactly that protection: if the config was
    changed meanwhile, the change becomes ``superseded`` and never applies. Apply
    is atomic — the new protection event row and the state transition commit
    together.

    Raises:
        ProtectionChangeNotFoundError: no such change for this asset and label.
        MoveStateError: the change is not pending, or was superseded now.
        KeyNotFoundError / SignerVerificationError / SelfApprovalError: as
            :func:`approve_move`.
    """
    validate_label_name(label)
    trust = trust_list or load_signer_trust_list()
    conn = _open(db_path)
    try:
        row = _get_change_row(conn, asset_name, change_id)
        if row is None or row["label"] != label:
            raise ProtectionChangeNotFoundError(
                f"No protection change '{change_id}' found for label '{label}' on "
                f"'{asset_name}'."
            )
        if row["state"] != "pending":
            raise MoveStateError(
                f"Protection change '{change_id}' is '{row['state']}' (terminal) — "
                "propose a new change instead."
            )

        approver = identity or getpass.getuser()
        private_key, key_fp = ensure_keypair(approver, create=False)
        if key_fp == row["proposer_key_fp"] or approver == row["proposed_by"]:
            raise SelfApprovalError(
                "SoD violation: the checker of a protection change must be a different "
                "identity with a different keypair than its maker (ADR-0114)."
            )

        maker: TrustedSigner | None = None
        if not reject:
            maker = _verify_change_maker(trust, row)

        decision = "reject" if reject else "approve"
        approved_at = _now()
        payload = protection_approval_payload(
            change_id, decision, approver, approved_at, str(row["proposer_sig"])
        )
        sig = sign_payload(private_key, payload)
        checker = trust.verify(
            claimed_identity=approver,
            key_fp=key_fp,
            role="approver",
            payload=payload,
            signature=sig,
            party="approver",
        )
        if maker is not None and (
            checker.identity == maker.identity or checker.fingerprint == maker.fingerprint
        ):
            raise SelfApprovalError(
                f"SoD violation: maker and checker resolve to the same bound identity "
                f"{maker.identity!r} in the signer trust list."
            )
        current = _protection_row(conn, asset_name, label)
        if not reject and (current is None or current["event_id"] != row["base_event_id"]):
            conn.execute(
                "UPDATE asset_label_protection_changes SET state = 'superseded' "
                "WHERE change_id = ?",
                (change_id,),
            )
            conn.commit()
            raise MoveStateError(
                f"The protection of '{label}' on '{asset_name}' changed after protection "
                f"change '{change_id}' was proposed; it is now superseded and will never "
                "apply. Propose a new change against the current protection."
            )
        conn.execute(
            """
            INSERT INTO asset_label_protection_change_approvals
                (approval_id, change_id, approver, approver_key_fp, approver_sig,
                 decision, note, approved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_ulid(), change_id, checker.identity, checker.fingerprint, sig,
                decision, note, approved_at,
            ),
        )
        if reject:
            conn.execute(
                "UPDATE asset_label_protection_changes SET state = 'rejected' "
                "WHERE change_id = ?",
                (change_id,),
            )
            conn.commit()
            return _refreshed_change(conn, asset_name, change_id), "rejected"

        assert maker is not None  # verified above for every non-reject decision
        distinct = _verified_change_approvers(conn, trust, row, maker)
        required: int = row["base_required_approvals"]
        if len(distinct) < required:
            conn.commit()
            detail = f"pending ({len(distinct)}/{required} distinct approvals)"
            return _refreshed_change(conn, asset_name, change_id), detail

        # ---- atomic apply: new protection event + state transition, one commit ----
        applied_at = _now()
        event_id = new_ulid()
        approvers = ", ".join(sorted(distinct))
        applied_note = (
            f"{row['note'] + ' ' if row['note'] else ''}"
            f"[maker-checker protection change {change_id}; approved by {approvers}]"
        )
        conn.execute(
            """
            INSERT INTO asset_label_protection
                (event_id, schema_version, asset_name, label, protected,
                 required_approvals, policy_ref, note, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                "0.1.0",
                asset_name,
                label,
                row["protected"],
                row["required_approvals"],
                row["policy_ref"],
                applied_note,
                row["proposed_by"],
                applied_at,
            ),
        )
        conn.execute(
            "UPDATE asset_label_protection_changes "
            "SET state = 'applied', applied_at = ?, applied_event_id = ? "
            "WHERE change_id = ?",
            (applied_at, event_id, change_id),
        )
        conn.commit()
        return _refreshed_change(conn, asset_name, change_id), "applied"
    finally:
        conn.close()


def _refreshed_change(
    conn: sqlite3.Connection, asset_name: str, change_id: str
) -> dict[str, Any]:
    row = _get_change_row(conn, asset_name, change_id)
    assert row is not None  # no_delete trigger guarantees the row remains
    return _change_record(conn, row)


def find_protection_change(
    asset_name: str, change_id: str, *, db_path: Path | None = None
) -> dict[str, Any] | None:
    """One protection-change record by id, or ``None`` if there is none."""
    conn = _open(db_path)
    try:
        row = _get_change_row(conn, asset_name, change_id)
        return None if row is None else _change_record(conn, row)
    finally:
        conn.close()


def list_protection_changes(
    asset_name: str,
    label: str | None = None,
    *,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Protection-change records for an asset (newest first), optionally one label."""
    conn = _open(db_path)
    try:
        query = "SELECT * FROM asset_label_protection_changes WHERE asset_name = ?"
        params: list[str] = [asset_name]
        if label is not None:
            validate_label_name(label)
            query += " AND label = ?"
            params.append(label)
        query += " ORDER BY proposed_at DESC, rowid DESC"
        return [_change_record(conn, r) for r in conn.execute(query, params).fetchall()]
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
    trust_list: SignerTrustList | None = None,
) -> dict[str, Any]:
    """Propose a protected-label move (maker step) — the label does NOT move.

    Fail-closed: the target version must exist; the label must actually be
    protected (free labels use :func:`~novafabric.registry.labels.set_label`);
    at most one non-terminal pending move per ``(asset_name, label)``. The
    proposal is Ed25519-signed with the maker's **existing** keyring key and the
    signature must verify against the signer trust list for the ``proposer`` role
    (ADR-0312); the bound identity and fingerprint are what get stored.

    Raises:
        KeyNotFoundError: no keyring key exists for *identity* (none is created —
            ``nova promote keygen`` is the only creation route).
        SignerVerificationError: trust list missing/malformed, key unlisted or bound
            to another identity, ``proposer`` role missing (``party="proposer"``).
    """
    validate_label_name(label)
    if label == RESERVED_LABEL_LATEST:
        raise ReservedLabelError(
            "'latest' is auto-maintained by the registry and cannot be moved."
        )
    # Fail closed before touching the registry: no trust list, no maker-checker.
    trust = trust_list or load_signer_trust_list()
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
        private_key, key_fp = ensure_keypair(proposer, create=False)
        move_id = new_ulid()
        proposed_at = _now()
        payload = proposal_payload(
            move_id, asset_name, label, from_version, proposed_version, proposed_at
        )
        sig = sign_payload(private_key, payload)
        maker = trust.verify(
            claimed_identity=proposer,
            key_fp=key_fp,
            role="proposer",
            payload=payload,
            signature=sig,
            party="proposer",
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
                maker.identity,
                maker.fingerprint,
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


def _verify_maker(trust: SignerTrustList, row: sqlite3.Row) -> TrustedSigner:
    """Re-verify the stored maker signature against the trust list (``proposer`` role).

    Refuses a move whose row was edited after proposing, whose maker has since
    been removed from the list, or that was proposed before the trust-list
    binding (an unlisted, possibly auto-minted key).
    """
    return trust.verify(
        claimed_identity=str(row["proposed_by"]),
        key_fp=str(row["proposer_key_fp"]),
        role="proposer",
        payload=proposal_payload(
            row["move_id"],
            row["asset_name"],
            row["label"],
            row["from_version"],
            row["proposed_version"],
            row["proposed_at"],
        ),
        signature=str(row["proposer_sig"]),
        party="proposer",
    )


def _verified_approvers(
    conn: sqlite3.Connection, trust: SignerTrustList, move_id: str, maker: TrustedSigner
) -> set[str]:
    """Bound identities of the stored ``approve`` decisions that verify today.

    An approval that does not verify against the current trust list (written
    before the binding, signed by a key since removed, or edited) is kept in the
    append-only table but never counted; neither is one that resolves to the maker.
    """
    identities: set[str] = set()
    for a in conn.execute(
        "SELECT approver, approver_key_fp, approver_sig, approved_at "
        "FROM asset_label_move_approvals WHERE move_id = ? AND decision = 'approve'",
        (move_id,),
    ):
        try:
            signer = trust.verify(
                claimed_identity=str(a["approver"]),
                key_fp=str(a["approver_key_fp"]),
                role="approver",
                payload=approval_payload(
                    move_id, "approve", str(a["approver"]), str(a["approved_at"])
                ),
                signature=str(a["approver_sig"]),
                party="approver",
            )
        except SignerVerificationError as exc:
            logger.warning(
                "protected-label move %s: stored approval by %r not counted: %s",
                move_id, a["approver"], exc,
            )
            continue
        if signer.identity == maker.identity or signer.fingerprint == maker.fingerprint:
            continue
        identities.add(signer.identity)
    return identities


def approve_move(
    asset_name: str,
    label: str,
    move_id: str,
    *,
    identity: str | None = None,
    reject: bool = False,
    note: str | None = None,
    db_path: Path | None = None,
    trust_list: SignerTrustList | None = None,
) -> tuple[dict[str, Any], str]:
    """Record a checker decision; apply the move when the gate is satisfied.

    Returns ``(record, detail)`` where ``detail`` is a human-readable outcome
    ("applied", "rejected", "pending (…)").

    The checker signs with an **existing** keyring key (none is created here)
    that the signer trust list binds to *identity* with the ``approver`` role
    (ADR-0312). For an approval the stored maker signature is re-verified first,
    so an edited move or a maker removed from the list is refused. SoD compares
    the **bound** identities and key fingerprints (mirrors ``approve_promotion``).
    Only approvals that verify against the trust list count toward
    ``required_approvals``, once per bound identity. A rejection skips the maker
    re-verification — a trusted checker can always retire an unverifiable
    (pre-fix) move — but still needs a trusted, distinct checker.

    Apply is atomic: the ADR-0113 ``asset_label_history`` row (reusing this
    move's ULID) and the state transition commit together.

    Raises:
        KeyNotFoundError: no keyring key exists for *identity*.
        SignerVerificationError: trust list missing/malformed, or a signature or
            binding fails (``exc.party`` names the side).
        SelfApprovalError: maker and checker share an identity or a key.
    """
    validate_label_name(label)
    trust = trust_list or load_signer_trust_list()
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
        private_key, key_fp = ensure_keypair(approver, create=False)
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

        maker: TrustedSigner | None = None
        if not reject:
            maker = _verify_maker(trust, row)

        decision = "reject" if reject else "approve"
        approved_at = _now()
        payload = approval_payload(move_id, decision, approver, approved_at)
        sig = sign_payload(private_key, payload)
        checker = trust.verify(
            claimed_identity=approver,
            key_fp=key_fp,
            role="approver",
            payload=payload,
            signature=sig,
            party="approver",
        )
        if maker is not None and (
            checker.identity == maker.identity or checker.fingerprint == maker.fingerprint
        ):
            raise SelfApprovalError(
                f"SoD violation: maker and checker resolve to the same bound identity "
                f"{maker.identity!r} in the signer trust list — they must be different "
                "people with distinct keypairs."
            )
        conn.execute(
            """
            INSERT INTO asset_label_move_approvals
                (approval_id, move_id, approver, approver_key_fp, approver_sig,
                 decision, note, approved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_ulid(), move_id, checker.identity, checker.fingerprint, sig,
                decision, note, approved_at,
            ),
        )

        if reject:
            conn.execute(
                "UPDATE asset_label_pending_moves SET state = 'rejected' "
                "WHERE move_id = ?",
                (move_id,),
            )
            conn.commit()
            return _refreshed(conn, asset_name, move_id), "rejected"

        assert maker is not None  # verified above for every non-reject decision
        distinct = _verified_approvers(conn, trust, move_id, maker)
        required: int = row["required_approvals"]
        if len(distinct) < required:
            conn.commit()
            detail = f"pending ({len(distinct)}/{required} distinct approvals)"
            return _refreshed(conn, asset_name, move_id), detail

        allow, reason = _policy_gate(_move_record(conn, row), checker.identity)
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
    """Protection config, current pointers, pending moves and protection changes."""
    protections = list_protections(asset_name, db_path=db_path)
    pointers = _labels.list_labels(asset_name, db_path=db_path)
    moves = list_moves(asset_name, label, db_path=db_path)
    changes = list_protection_changes(asset_name, label, db_path=db_path)
    if label is not None:
        protections = [p for p in protections if p["label"] == label]
        pointers = [p for p in pointers if p["label"] == label]
    return {
        "asset_name": asset_name,
        "protections": protections,
        "pointers": pointers,
        "pending_moves": moves,
        "protection_changes": changes,
    }
