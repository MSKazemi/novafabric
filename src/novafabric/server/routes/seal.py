"""NovaSeal maker-checker route — /v0/seal.

Implements:
  GET  /v0/seal/policy                     latest promotion policy (or 404)
  GET  /v0/seal/{capsule_id}/proposals     list proposals with status
  POST /v0/seal/{capsule_id}/verify        run five-check SoD verifier
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.exceptions import BundleNotFoundError, PolicyNotFoundError
from novafabric.promote.policy_store import PolicyStore
from novafabric.promote.predicates import (
    APPROVAL_PAYLOAD_TYPE,
    PROPOSAL_PAYLOAD_TYPE,
    EnvelopeError,
    verify_promote_envelope,
)
from novafabric.promote.verifier import verify_sod
from novafabric.server.auth import AuthContext
from novafabric.server.errors import NotFoundError
from novafabric.server.rbac import Role, require_role

router = APIRouter(prefix="/seal", tags=["seal"])

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PolicyResponse(BaseModel):
    version: int
    predicate: dict[str, Any]
    created_at: str


class ProposalSummary(BaseModel):
    uuid: str
    capsule_id: str
    proposer_subject: str
    justification: str
    timestamp: str
    policy_version: str
    has_approval: bool
    approver_subject: str | None = None
    approval_timestamp: str | None = None


class VerifyResponse(BaseModel):
    capsule_id: str
    passed: bool
    exit_code: int
    message: str
    check_results: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _data_dir() -> Path:
    """Return the NovaSeal data directory (base for PromoteBundleStore)."""
    import os

    env = os.environ.get("NOVAFABRIC_HOME")
    base = Path(env) if env else Path.home() / ".novafabric"
    return base


def _merkle_db_path() -> Path:
    """Return the NovaSeal Merkle log DB path (used by PolicyStore)."""
    from novafabric.trust.novaseal.config import resolve_merkle_db_path
    return resolve_merkle_db_path()


# ---------------------------------------------------------------------------
# GET /v0/seal/policy
# ---------------------------------------------------------------------------


@router.get("/policy", response_model=PolicyResponse)
async def get_seal_policy(
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> PolicyResponse:
    """Return the latest promotion policy predicate, or 404 if none exists."""
    db_path = _merkle_db_path()
    policy_store = PolicyStore(db_path)
    try:
        try:
            version = policy_store.get_latest_version()
            bundle_json = policy_store.get_latest()
        except PolicyNotFoundError:
            raise NotFoundError("No promotion policy found — run `nova policy sign` to create one.")
    finally:
        policy_store.close()

    # bundle_json may be a raw predicate or a DSSE envelope
    bundle = json.loads(bundle_json)
    if "payloadType" in bundle and "payload" in bundle:
        raw = bundle["payload"]
        pad = 4 - len(raw) % 4
        if pad != 4:
            raw += "=" * pad
        predicate: dict[str, Any] = json.loads(base64.urlsafe_b64decode(raw))
    else:
        predicate = bundle

    # Retrieve the created_at from the DB row
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT created_at FROM promote_policy "
            "WHERE namespace = 'default' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        created_at = str(row[0]) if row else ""
    finally:
        conn.close()

    return PolicyResponse(
        version=version,
        predicate=predicate,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# GET /v0/seal/{capsule_id}/proposals
# ---------------------------------------------------------------------------


@router.get("/{capsule_id}/proposals", response_model=list[ProposalSummary])
async def list_seal_proposals(
    capsule_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> list[ProposalSummary]:
    """List all proposals for a capsule with their approval status."""
    data_dir = _data_dir()
    bundle_store = PromoteBundleStore(data_dir)

    proposal_uuids = bundle_store.list_proposals(capsule_id)
    summaries: list[ProposalSummary] = []

    for proposal_uuid in proposal_uuids:
        # Default values if parsing fails
        proposer_subject = ""
        justification = ""
        timestamp = ""
        policy_version = ""
        has_approval = False
        approver_subject: str | None = None
        approval_timestamp: str | None = None

        try:
            proposal_bytes = bundle_store.get_proposal(capsule_id, proposal_uuid)
            payload_bytes, ps = verify_promote_envelope(proposal_bytes, PROPOSAL_PAYLOAD_TYPE)
            proposer_subject = ps
            predicate = json.loads(payload_bytes)
            justification = predicate.get("justification", "")
            timestamp = predicate.get("timestamp", "")
            policy_version = str(predicate.get("policy_version", ""))
        except (BundleNotFoundError, EnvelopeError, json.JSONDecodeError):
            # Envelope unreadable — still include with defaults
            pass

        # Check for approval
        try:
            approval_bytes = bundle_store.get_approval(capsule_id, proposal_uuid)
            apayload_bytes, as_ = verify_promote_envelope(approval_bytes, APPROVAL_PAYLOAD_TYPE)
            has_approval = True
            approver_subject = as_
            apred = json.loads(apayload_bytes)
            approval_timestamp = apred.get("timestamp")
        except (BundleNotFoundError, EnvelopeError, json.JSONDecodeError):
            has_approval = False

        summaries.append(
            ProposalSummary(
                uuid=proposal_uuid,
                capsule_id=capsule_id,
                proposer_subject=proposer_subject,
                justification=justification,
                timestamp=timestamp,
                policy_version=policy_version,
                has_approval=has_approval,
                approver_subject=approver_subject,
                approval_timestamp=approval_timestamp,
            )
        )

    return summaries


# ---------------------------------------------------------------------------
# POST /v0/seal/{capsule_id}/verify
# ---------------------------------------------------------------------------

_CHECK_NAMES = [
    "Proposal signature + proposer in policy",
    "Approval signature + approver in policy",
    "proposal_digest match",
    "No self-approval",
    "Timestamp ordering (approval > proposal)",
]

# exit_code → which check failed (0-indexed)
_EXIT_CODE_TO_CHECK: dict[int, int] = {3: 0, 4: 1, 5: 2, 6: 3, 7: 4}


@router.post("/{capsule_id}/verify", response_model=VerifyResponse)
async def verify_seal_chain(
    capsule_id: str,
    _auth: Annotated[AuthContext, Depends(require_role(Role.reader))] = None,  # type: ignore[assignment]
) -> VerifyResponse:
    """Run the five-check SoD verifier for a capsule's promote bundles."""
    data_dir = _data_dir()
    db_path = _merkle_db_path()

    bundle_store = PromoteBundleStore(data_dir)
    policy_store = PolicyStore(db_path)

    try:
        result = verify_sod(capsule_id, policy_store, bundle_store)
    finally:
        policy_store.close()

    check_results: list[dict[str, Any]]
    if result.passed:
        # All 5 checks passed
        check_results = [
            {"check": i + 1, "name": name, "passed": True, "message": "ok"}
            for i, name in enumerate(_CHECK_NAMES)
        ]
    else:
        failed_check_idx = _EXIT_CODE_TO_CHECK.get(result.exit_code, -1)
        check_results = []
        for i, name in enumerate(_CHECK_NAMES):
            if i < failed_check_idx:
                check_results.append(
                    {"check": i + 1, "name": name, "passed": True, "message": "ok"}
                )
            elif i == failed_check_idx:
                check_results.append(
                    {"check": i + 1, "name": name, "passed": False, "message": result.message}
                )
            else:
                # Not reached
                break

        # If exit_code is 8 (missing approval) or 9 (missing proposal), no check entries
        if failed_check_idx == -1:
            check_results = []

    return VerifyResponse(
        capsule_id=capsule_id,
        passed=result.passed,
        exit_code=result.exit_code,
        message=result.message,
        check_results=check_results,
    )
