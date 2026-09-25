"""Read-only collectors for the NF-276 validation-independence exporter (ADR-0159 D2).

These functions *read* the two shipped ADR-0058 maker-checker stores and return
:class:`~.model_independence.MakerCheckerRecord` values; they never re-implement the gate:

* the registry ``promotion_proposals`` table written by ``nova promote propose`` /
  ``nova promote approve`` (:func:`novafabric.registry.service.propose_promotion` /
  :func:`~novafabric.registry.service.approve_promotion`) — opened **read-only**
  (``mode=ro``) so an export never creates or migrates a database;
* the capsule-scoped DSSE proposal/approval bundles written by ``nova seal propose`` /
  ``nova seal approve`` — whose SoD outcome is taken verbatim from
  :func:`novafabric.promote.verifier.verify_sod`, the shipped five-check verifier.

The single-sign-off ``approvals`` table (``nova approve``) is deliberately *not* read: it records
one approver and no developer identity, so it cannot evidence independence either way.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .model_independence import MakerCheckerRecord

if TYPE_CHECKING:
    from novafabric.promote.policy_store import PolicyStore

#: verify_sod exit codes (see :func:`novafabric.promote.verifier.verify_sod`).
_SOD_PASS = 0
_SOD_SELF_APPROVAL = 6
_SOD_NO_APPROVAL = 8
_SOD_NO_PROPOSAL = 9


class MakerCheckerSourceError(Exception):
    """A maker-checker store exists but is unreadable or corrupt (CLI exit 2, never a gap)."""


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only.

    The path is URI-escaped (``Path.as_uri``) so a ``?`` or ``#`` in it can neither truncate the
    filename nor drop ``mode=ro`` — which would silently create a fresh, empty database.
    """
    return sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)


class _ReadOnlyPolicySnapshot:
    """The ``promote_policy`` rows of a policy database, read once via a ``mode=ro`` connection.

    Duck-types the single :class:`~novafabric.promote.policy_store.PolicyStore` method
    :func:`~novafabric.promote.verifier.verify_sod` calls (``get_by_version``), with the same
    semantics, so the verifier runs unchanged — but, unlike ``PolicyStore(path)``, it never
    creates the file, its directory, the table, or flips the database into WAL mode.
    """

    def __init__(self, db_path: Path) -> None:
        self._rows: dict[tuple[str, int], str] = {}
        if not db_path.exists():
            return
        try:
            conn = _connect_ro(db_path)
            try:
                rows = conn.execute(
                    "SELECT namespace, version, bundle_json FROM promote_policy"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return
            raise MakerCheckerSourceError(f"cannot read policy db {db_path}: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise MakerCheckerSourceError(f"cannot read policy db {db_path}: {exc}") from exc
        for namespace, version, bundle_json in rows:
            self._rows.setdefault((str(namespace), int(version)), str(bundle_json))

    def get_by_version(self, version: int, namespace: str = "default") -> str:
        """Return bundle_json for ``version``; raises ``PolicyNotFoundError`` (as PolicyStore)."""
        from novafabric.promote.exceptions import PolicyNotFoundError

        try:
            return self._rows[(namespace, version)]
        except KeyError:
            raise PolicyNotFoundError(
                f"No policy version {version} in namespace {namespace!r}"
            ) from None

    def close(self) -> None:
        """No-op: the connection is closed as soon as the rows are read."""


def split_model_id(model_id: str) -> tuple[str, str | None]:
    """Split ``name[@version]`` into ``(name, version-or-None)``.

    Raises:
        ValueError: the name or the version part is empty.
    """
    name, sep, version = model_id.rpartition("@")
    if not sep:
        name, version = model_id, ""
    if not name or (sep and not version):
        raise ValueError(f"invalid model id {model_id!r}; expected name or name@version")
    return name, (version or None)


def collect_registry_records(db_path: Path, model_id: str) -> list[MakerCheckerRecord]:
    """Read ADR-0058 promotion proposals for ``model_id`` from the registry, read-only.

    A missing database or a database without the ``promotion_proposals`` table means *no record*
    (an empty list — rendered as ``missing``), never an error.

    Raises:
        ValueError: ``model_id`` is malformed.
        MakerCheckerSourceError: the database file exists but cannot be read.
    """
    name, version = split_model_id(model_id)
    if not db_path.exists():
        return []
    sql = "SELECT * FROM promotion_proposals WHERE asset_name = ?"
    params: tuple[str, ...] = (name,)
    if version is not None:
        sql += " AND asset_version = ?"
        params = (name, version)
    sql += " ORDER BY proposed_at, proposal_id"
    try:
        conn = _connect_ro(db_path)
    except sqlite3.Error as exc:  # pragma: no cover - open is lazy; errors surface on execute
        raise MakerCheckerSourceError(f"cannot open registry {db_path}: {exc}") from exc
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise MakerCheckerSourceError(f"cannot read registry {db_path}: {exc}") from exc
    except sqlite3.DatabaseError as exc:
        raise MakerCheckerSourceError(f"cannot read registry {db_path}: {exc}") from exc
    finally:
        conn.close()
    return [
        MakerCheckerRecord(
            source="registry_promotion",
            record_ref=f"registry://promotion_proposals/{row['proposal_id']}",
            subject=f"{row['asset_name']}@{row['asset_version']}",
            state=str(row["state"]),
            maker=row["proposer"],
            checker=row["approver"],
            maker_key_fp=row["proposer_key_fp"],
            checker_key_fp=row["approver_key_fp"],
            proposed_at=row["proposed_at"],
            approved_at=row["approved_at"],
        )
        for row in rows
    ]


def _envelope_subject(envelope: bytes, payload_type: str) -> tuple[str | None, str | None]:
    """Return ``(signer subject, predicate timestamp)`` of a promote envelope, or Nones.

    Uses the shipped :func:`~novafabric.promote.predicates.verify_promote_envelope`; an envelope
    that does not verify yields no identity (never a guessed one).
    """
    from novafabric.promote.predicates import EnvelopeError, verify_promote_envelope

    try:
        payload, subject = verify_promote_envelope(envelope, payload_type)
    except EnvelopeError:
        return None, None
    try:
        ts = json.loads(payload).get("timestamp")
    except (ValueError, AttributeError):
        ts = None
    return subject, (str(ts) if ts is not None else None)


def collect_seal_record(
    capsule_id: str, *, data_dir: Path, policy_db: Path
) -> MakerCheckerRecord | None:
    """Read the DSSE maker-checker bundle for ``capsule_id`` and its ``verify_sod`` outcome.

    Returns ``None`` when no proposal is stored for the capsule (no record). The policy database
    is read through a ``mode=ro`` snapshot (never ``PolicyStore``, whose constructor creates the
    file, runs DDL and switches it to WAL); when it is absent the snapshot is empty, so the
    verifier reports the policy gap itself rather than this module creating a database.

    Raises:
        MakerCheckerSourceError: the policy database exists but cannot be read.
    """
    from novafabric.promote.bundle_store import PromoteBundleStore
    from novafabric.promote.exceptions import BundleNotFoundError
    from novafabric.promote.predicates import APPROVAL_PAYLOAD_TYPE, PROPOSAL_PAYLOAD_TYPE
    from novafabric.promote.verifier import verify_sod

    bundle_store = PromoteBundleStore(data_dir)
    proposals = bundle_store.list_proposals(capsule_id)
    has_bypass = bool(bundle_store.list_bypasses(capsule_id))
    if not proposals and not has_bypass:
        return None
    policy_store = _ReadOnlyPolicySnapshot(policy_db)
    result = verify_sod(capsule_id, cast("PolicyStore", policy_store), bundle_store, offline=True)
    if result.exit_code == _SOD_NO_PROPOSAL:
        return None

    maker = checker = proposed_at = approved_at = None
    proposal_uuid = proposals[0] if proposals else "bypass"
    if proposals:
        maker, proposed_at = _envelope_subject(
            bundle_store.get_proposal(capsule_id, proposal_uuid), PROPOSAL_PAYLOAD_TYPE
        )
        try:
            approval = bundle_store.get_approval(capsule_id, proposal_uuid)
        except BundleNotFoundError:
            approval = None
        if approval is not None:
            checker, approved_at = _envelope_subject(approval, APPROVAL_PAYLOAD_TYPE)

    failure: str | None = None
    if result.bypass_used:
        state = "bypassed"
    elif result.exit_code == _SOD_PASS:
        state = "verified"
    elif result.exit_code == _SOD_NO_APPROVAL:
        state, checker = "open", None
    elif result.exit_code == _SOD_SELF_APPROVAL:
        state = "self-approved"
    else:
        state, failure = "unverified", result.message
    return MakerCheckerRecord(
        source="seal_promote_bundle",
        record_ref=f"seal://promote/{capsule_id}/{proposal_uuid}",
        subject=capsule_id,
        state=state,
        maker=maker,
        checker=checker,
        proposed_at=proposed_at,
        approved_at=approved_at,
        bypass_used=result.bypass_used,
        verification_failure=failure,
    )
