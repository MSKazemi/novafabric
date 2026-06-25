"""Five-check SoD verifier for capsule promote bundles."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

import jcs  # type: ignore[import-untyped]

from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.exceptions import BundleNotFoundError, PolicyNotFoundError
from novafabric.promote.policy_store import PolicyStore
from novafabric.promote.predicates import (
    APPROVAL_PAYLOAD_TYPE,
    BYPASS_PAYLOAD_TYPE,
    PROPOSAL_PAYLOAD_TYPE,
    EnvelopeError,
    verify_promote_envelope,
)


@dataclass
class VerifyResult:
    passed: bool
    exit_code: int  # 0=pass; 3-7 per check; 8=missing approval; 9=missing proposal
    message: str
    bypass_used: bool = field(default=False)


def verify_sod(
    capsule_id: str,
    policy_store: PolicyStore,
    bundle_store: PromoteBundleStore,
    offline: bool = False,
) -> VerifyResult:
    """Run the five-check SoD verification for a capsule's promote bundles.

    Checks (stop at first failure):
      1. Proposal signature valid; proposer_subject in policy.proposer_key_ids  → exit 3
      2. Approval signature valid; approver_subject in policy.approver_key_ids  → exit 4
      3. proposal_digest in approval == SHA-256(JCS(proposal_envelope))         → exit 5
      4. approver_subject != proposer_subject (no self-approval)                → exit 6
      5. approval.timestamp > proposal.timestamp                                → exit 7

    Missing approval → exit 8
    Missing proposal → exit 9
    """
    # ------------------------------------------------------------------
    # Bypass check — if a valid (non-expired) bypass exists, skip SoD
    # ------------------------------------------------------------------
    bypass_uuids = bundle_store.list_bypasses(capsule_id)
    now = datetime.now(UTC)
    for bypass_uuid in bypass_uuids:
        try:
            valid_until_str = bundle_store.get_bypass_valid_until(capsule_id, bypass_uuid)
            valid_until = datetime.fromisoformat(valid_until_str)
            if valid_until.tzinfo is None:
                valid_until = valid_until.replace(tzinfo=UTC)
            if valid_until > now:
                bypass_env = bundle_store.get_bypass(capsule_id, bypass_uuid)
                try:
                    _, bypass_subject = verify_promote_envelope(bypass_env, BYPASS_PAYLOAD_TYPE)
                except EnvelopeError:
                    continue  # Invalid signature — try next bypass
                msg = f"bypass active until {valid_until_str} (authorized by {bypass_subject})"
                return VerifyResult(passed=True, exit_code=0, message=msg, bypass_used=True)
        except BundleNotFoundError:
            continue

    # ------------------------------------------------------------------
    # Load proposal
    # ------------------------------------------------------------------
    proposal_uuids = bundle_store.list_proposals(capsule_id)
    if not proposal_uuids:
        msg = f"No proposal found for capsule {capsule_id!r}"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=9, message=msg)

    proposal_uuid = proposal_uuids[0]

    try:
        proposal_envelope_bytes = bundle_store.get_proposal(capsule_id, proposal_uuid)
    except BundleNotFoundError as exc:  # pragma: no cover — TOCTOU: list succeeded but get failed
        msg = str(exc)
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=9, message=msg)

    # ------------------------------------------------------------------
    # Check 1: Verify proposal envelope + proposer in policy
    # ------------------------------------------------------------------
    try:
        proposal_payload, proposer_subject = verify_promote_envelope(
            proposal_envelope_bytes, PROPOSAL_PAYLOAD_TYPE
        )
    except EnvelopeError as exc:
        msg = f"proposal signature invalid: {exc}"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=3, message=msg)

    proposal_predicate = json.loads(proposal_payload)
    policy_version_str = proposal_predicate.get("policy_version", "")

    try:
        policy_version = int(policy_version_str)
        policy_json = policy_store.get_by_version(policy_version)
    except (ValueError, PolicyNotFoundError) as exc:
        msg = f"policy not found (version={policy_version_str!r}): {exc}"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=3, message=msg)

    policy = json.loads(policy_json)
    # policy_json may be a DSSE envelope (if policy sign was used) or raw predicate
    # Unwrap if it's a DSSE envelope
    if "payloadType" in policy and "payload" in policy:
        import base64
        raw_payload = policy["payload"]
        pad = 4 - len(raw_payload) % 4
        if pad != 4:
            raw_payload += "=" * pad
        policy = json.loads(base64.urlsafe_b64decode(raw_payload))

    proposer_key_ids: list[str] = policy.get("proposer_key_ids", [])
    if proposer_subject not in proposer_key_ids:
        msg = f"proposer key {proposer_subject!r} not in policy proposer_key_ids"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=3, message=msg)

    # ------------------------------------------------------------------
    # Load approval
    # ------------------------------------------------------------------
    try:
        approval_envelope_bytes = bundle_store.get_approval(capsule_id, proposal_uuid)
    except BundleNotFoundError as exc:
        msg = str(exc)
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=8, message=msg)

    # ------------------------------------------------------------------
    # Check 2: Verify approval envelope + approver in policy
    # ------------------------------------------------------------------
    try:
        approval_payload, approver_subject = verify_promote_envelope(
            approval_envelope_bytes, APPROVAL_PAYLOAD_TYPE
        )
    except EnvelopeError as exc:
        msg = f"approval signature invalid: {exc}"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=4, message=msg)

    approval_predicate = json.loads(approval_payload)

    approver_key_ids: list[str] = policy.get("approver_key_ids", [])
    if approver_subject not in approver_key_ids:
        msg = f"approver key {approver_subject!r} not in policy approver_key_ids"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=4, message=msg)

    # ------------------------------------------------------------------
    # Check 3: proposal_digest match
    # ------------------------------------------------------------------
    canonical = jcs.canonicalize(json.loads(proposal_envelope_bytes))
    expected_digest = hashlib.sha256(canonical).hexdigest()
    actual_digest = approval_predicate.get("proposal_digest", {}).get("sha256", "")

    if actual_digest != expected_digest:
        msg = "proposal_digest mismatch"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=5, message=msg)

    # ------------------------------------------------------------------
    # Check 4: No self-approval
    # ------------------------------------------------------------------
    if approver_subject == proposer_subject:
        msg = "self-approval prohibited"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=6, message=msg)

    # ------------------------------------------------------------------
    # Check 5: Timestamp ordering
    # ------------------------------------------------------------------
    proposal_ts = proposal_predicate.get("timestamp", "")
    approval_ts = approval_predicate.get("timestamp", "")

    if approval_ts <= proposal_ts:
        msg = "timestamp ordering violation"
        print(msg, file=sys.stderr)
        return VerifyResult(passed=False, exit_code=7, message=msg)

    return VerifyResult(
        passed=True,
        exit_code=0,
        message="SoD verification passed",
    )
