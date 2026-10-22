"""Five-check SoD verifier for capsule promote bundles."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import jcs  # type: ignore[import-untyped]

from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.exceptions import BundleNotFoundError, PolicyNotFoundError
from novafabric.promote.policy_store import PolicyStore
from novafabric.promote.predicates import (
    APPROVAL_PAYLOAD_TYPE,
    BYPASS_PAYLOAD_TYPE,
    POLICY_PAYLOAD_TYPE,
    PROPOSAL_PAYLOAD_TYPE,
    EnvelopeError,
    PromoteTrust,
    verify_promote_envelope,
)

log = logging.getLogger(__name__)


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
    *,
    trust: PromoteTrust | None = None,
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
    # Trust root (ADR-0315): every envelope below is signed by a certificate the
    # envelope itself carries, so its identity means nothing until it chains to a
    # pinned root. Unresolvable configuration fails closed.
    # ------------------------------------------------------------------
    try:
        if trust is None:
            trust = PromoteTrust.resolve()
        trust.require()
    except EnvelopeError as exc:
        return VerifyResult(passed=False, exit_code=3, message=str(exc))

    # ------------------------------------------------------------------
    # Bypass check — a valid, trusted, authorised, unexpired bypass skips SoD
    # ------------------------------------------------------------------
    bypass_msg = _active_bypass(capsule_id, policy_store, bundle_store, trust)
    if bypass_msg is not None:
        return VerifyResult(passed=True, exit_code=0, message=bypass_msg, bypass_used=True)

    # ------------------------------------------------------------------
    # Load proposal
    # ------------------------------------------------------------------
    proposal_uuids = bundle_store.list_proposals(capsule_id)
    if not proposal_uuids:
        msg = f"No proposal found for capsule {capsule_id!r}"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=9, message=msg)

    proposal_uuid = proposal_uuids[0]

    try:
        proposal_envelope_bytes = bundle_store.get_proposal(capsule_id, proposal_uuid)
    except BundleNotFoundError as exc:  # pragma: no cover — TOCTOU: list succeeded but get failed
        msg = str(exc)
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=9, message=msg)

    # ------------------------------------------------------------------
    # Check 1: Verify proposal envelope + proposer in policy
    # ------------------------------------------------------------------
    try:
        proposal_payload, proposer_subject = verify_promote_envelope(
            proposal_envelope_bytes, PROPOSAL_PAYLOAD_TYPE, trust=trust
        )
    except EnvelopeError as exc:
        msg = f"proposal signature invalid: {exc}"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=3, message=msg)

    proposal_predicate = json.loads(proposal_payload)
    policy_version_str = proposal_predicate.get("policy_version", "")

    try:
        policy_version = int(policy_version_str)
        policy = _load_policy(policy_store.get_by_version(policy_version), trust)
    except (ValueError, PolicyNotFoundError) as exc:
        msg = f"policy not found (version={policy_version_str!r}): {exc}"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=3, message=msg)
    except EnvelopeError as exc:
        msg = f"policy signature invalid (version={policy_version_str!r}): {exc}"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=3, message=msg)

    proposer_key_ids: list[str] = policy.get("proposer_key_ids", [])
    if proposer_subject not in proposer_key_ids:
        msg = f"proposer key {proposer_subject!r} not in policy proposer_key_ids"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=3, message=msg)

    # ------------------------------------------------------------------
    # Load approval
    # ------------------------------------------------------------------
    try:
        approval_envelope_bytes = bundle_store.get_approval(capsule_id, proposal_uuid)
    except BundleNotFoundError as exc:
        msg = str(exc)
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=8, message=msg)

    # ------------------------------------------------------------------
    # Check 2: Verify approval envelope + approver in policy
    # ------------------------------------------------------------------
    try:
        approval_payload, approver_subject = verify_promote_envelope(
            approval_envelope_bytes, APPROVAL_PAYLOAD_TYPE, trust=trust
        )
    except EnvelopeError as exc:
        msg = f"approval signature invalid: {exc}"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=4, message=msg)

    approval_predicate = json.loads(approval_payload)

    approver_key_ids: list[str] = policy.get("approver_key_ids", [])
    if approver_subject not in approver_key_ids:
        msg = f"approver key {approver_subject!r} not in policy approver_key_ids"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=4, message=msg)

    # ------------------------------------------------------------------
    # Check 3: proposal_digest match
    # ------------------------------------------------------------------
    canonical = jcs.canonicalize(json.loads(proposal_envelope_bytes))
    expected_digest = hashlib.sha256(canonical).hexdigest()
    actual_digest = approval_predicate.get("proposal_digest", {}).get("sha256", "")

    if actual_digest != expected_digest:
        msg = "proposal_digest mismatch"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=5, message=msg)

    # ------------------------------------------------------------------
    # Check 4: No self-approval
    # ------------------------------------------------------------------
    if approver_subject == proposer_subject:
        msg = "self-approval prohibited"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=6, message=msg)

    # ------------------------------------------------------------------
    # Check 5: Timestamp ordering
    # ------------------------------------------------------------------
    proposal_ts = proposal_predicate.get("timestamp", "")
    approval_ts = approval_predicate.get("timestamp", "")

    if approval_ts <= proposal_ts:
        msg = "timestamp ordering violation"
        log.warning(msg)
        return VerifyResult(passed=False, exit_code=7, message=msg)

    return VerifyResult(
        passed=True,
        exit_code=0,
        message="SoD verification passed",
    )


def _load_policy(policy_json: str, trust: PromoteTrust) -> dict[str, Any]:
    """Parse a stored policy; a signed (DSSE) policy must verify under ``trust``.

    Before ADR-0315 a signed policy was base64-unwrapped without checking its
    signature, so the signature added nothing. A raw (unsigned) predicate is still
    accepted: it is the operator's own local policy-store entry.

    Raises:
        EnvelopeError: a signed policy whose signature or signer does not verify.
    """
    policy: dict[str, Any] = json.loads(policy_json)
    if "payloadType" in policy and "payload" in policy:
        payload, _ = verify_promote_envelope(
            policy_json.encode("utf-8"), POLICY_PAYLOAD_TYPE, trust=trust
        )
        policy = json.loads(payload)
    return policy


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _active_bypass(
    capsule_id: str,
    policy_store: PolicyStore,
    bundle_store: PromoteBundleStore,
    trust: PromoteTrust,
) -> str | None:
    """Return a message for the first bypass that may skip SoD, else ``None``.

    A bypass counts only when (ADR-0315): its signature verifies and its signer chains
    to the promote trust root; the signed predicate names this ``capsule_id``; it is
    unexpired by **both** the signed ``valid_until`` and the store's copy (the store
    file is unsigned, so it may shorten a bypass but never extend it); and its signer
    is in the latest policy's ``approver_key_ids``. Anything else is skipped.
    """
    now = datetime.now(UTC)
    approvers: list[str] | None = None
    for bypass_uuid in bundle_store.list_bypasses(capsule_id):
        try:
            stored_until = _parse_utc(bundle_store.get_bypass_valid_until(capsule_id, bypass_uuid))
            envelope = bundle_store.get_bypass(capsule_id, bypass_uuid)
            payload, subject = verify_promote_envelope(envelope, BYPASS_PAYLOAD_TYPE, trust=trust)
            predicate = json.loads(payload)
        except (BundleNotFoundError, EnvelopeError, ValueError) as exc:
            log.warning("bypass %s for %s rejected: %s", bypass_uuid, capsule_id, exc)
            continue
        if not isinstance(predicate, dict) or predicate.get("capsule_id") != capsule_id:
            log.warning("bypass %s names a different capsule; ignored", bypass_uuid)
            continue
        signed_until = _parse_utc(predicate.get("valid_until"))
        if signed_until is None or stored_until is None or min(signed_until, stored_until) <= now:
            continue
        if approvers is None:
            try:
                approvers = list(_load_policy(policy_store.get_latest(), trust).get(
                    "approver_key_ids", []
                ))
            except (PolicyNotFoundError, EnvelopeError, ValueError) as exc:
                log.warning("bypass refused: no usable promote policy: %s", exc)
                return None
        if subject not in approvers:
            log.warning("bypass %s signer %r is not a policy approver", bypass_uuid, subject)
            continue
        until = min(signed_until, stored_until).isoformat()
        return f"bypass active until {until} (authorized by {subject})"
    return None
