"""Tests for the five-check SoD verifier."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import jcs
import pytest

from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.policy_store import PolicyStore
from novafabric.promote.predicates import (
    APPROVAL_PAYLOAD_TYPE,
    PROPOSAL_PAYLOAD_TYPE,
    build_approval_predicate,
    build_policy_predicate,
    build_proposal_predicate,
    sign_promote_envelope,
)
from novafabric.promote.verifier import verify_sod

FIXTURE_KEYS = Path(__file__).parent.parent / "fixtures" / "promote" / "keys"

CAPSULE_ID = "cap-verify-001"


def _policy_json(proposers: list[str], approvers: list[str]) -> str:
    return json.dumps(build_policy_predicate(proposers, approvers))


def _make_proposal_envelope(
    capsule_id: str,
    policy_version: str,
    key_path: Path,
    cert_path: Path,
    proposer_subject: str,
    justification: str = "This is a valid justification with sufficient length.",
) -> bytes:
    pred = build_proposal_predicate(
        capsule_id=capsule_id,
        capsule_digest_hex="a" * 64,
        target_env="staging",
        justification=justification,
        proposer_subject=proposer_subject,
        policy_version=policy_version,
    )
    payload = json.dumps(pred).encode()
    return sign_promote_envelope(payload, PROPOSAL_PAYLOAD_TYPE, key_path, cert_path)


def _make_approval_envelope(
    proposal_envelope: bytes,
    key_path: Path,
    cert_path: Path,
    approver_subject: str,
    decision: str = "approved",
) -> bytes:
    pred = build_approval_predicate(proposal_envelope, decision, approver_subject)
    payload = json.dumps(pred).encode()
    return sign_promote_envelope(payload, APPROVAL_PAYLOAD_TYPE, key_path, cert_path)


@pytest.fixture
def happy_path_stores(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
    approver_key_path: Path,
    approver_cert_path: Path,
) -> tuple[PolicyStore, PromoteBundleStore, str, str]:
    """Create a full happy-path setup: policy → proposal → approval."""
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    # Put policy
    policy_json = _policy_json(["proposer"], ["approver"])
    version = policy_store.put(policy_json)
    version_str = str(version)

    # Proposal
    proposal_env = _make_proposal_envelope(
        CAPSULE_ID, version_str,
        proposer_key_path, proposer_cert_path, "proposer",
    )
    proposal_uuid = bundle_store.put_proposal(CAPSULE_ID, proposal_env)

    # Small sleep to guarantee timestamp ordering
    time.sleep(0.05)

    # Approval
    approval_env = _make_approval_envelope(
        proposal_env, approver_key_path, approver_cert_path, "approver"
    )
    bundle_store.put_approval(CAPSULE_ID, approval_env, proposal_uuid)

    return policy_store, bundle_store, proposal_uuid, version_str


# ---------------------------------------------------------------------------
# tc-001: Full happy path
# ---------------------------------------------------------------------------


def test_verify_sod_pass(
    happy_path_stores: tuple[PolicyStore, PromoteBundleStore, str, str],
) -> None:
    policy_store, bundle_store, _, _ = happy_path_stores
    result = verify_sod(CAPSULE_ID, policy_store, bundle_store)
    assert result.passed is True
    assert result.exit_code == 0
    assert "passed" in result.message.lower()


def test_verify_sod_offline_flag(
    happy_path_stores: tuple[PolicyStore, PromoteBundleStore, str, str],
) -> None:
    """offline=True should still work (no network calls in v0.1)."""
    policy_store, bundle_store, _, _ = happy_path_stores
    result = verify_sod(CAPSULE_ID, policy_store, bundle_store, offline=True)
    assert result.passed is True
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Check 1 failure: proposer not in policy → exit 3
# ---------------------------------------------------------------------------


def test_verify_check1_proposer_not_in_policy(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
    approver_key_path: Path,
    approver_cert_path: Path,
) -> None:
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    # Policy only allows "admin" as proposer, but we sign with "proposer" CN
    policy_json = _policy_json(["admin"], ["approver"])
    version = policy_store.put(policy_json)

    proposal_env = _make_proposal_envelope(
        CAPSULE_ID, str(version), proposer_key_path, proposer_cert_path, "proposer"
    )
    bundle_store.put_proposal(CAPSULE_ID, proposal_env)

    result = verify_sod(CAPSULE_ID, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# Check 2 failure: approver not in policy → exit 4
# ---------------------------------------------------------------------------


def test_verify_check2_approver_not_in_policy(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
    approver_key_path: Path,
    approver_cert_path: Path,
) -> None:
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    # Policy only allows "admin" as approver, but we sign with "approver" CN
    policy_json = _policy_json(["proposer"], ["admin"])
    version = policy_store.put(policy_json)

    proposal_env = _make_proposal_envelope(
        CAPSULE_ID, str(version), proposer_key_path, proposer_cert_path, "proposer"
    )
    proposal_uuid = bundle_store.put_proposal(CAPSULE_ID, proposal_env)

    time.sleep(0.05)
    approval_env = _make_approval_envelope(
        proposal_env, approver_key_path, approver_cert_path, "approver"
    )
    bundle_store.put_approval(CAPSULE_ID, approval_env, proposal_uuid)

    result = verify_sod(CAPSULE_ID, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 4


# ---------------------------------------------------------------------------
# Check 3 failure: proposal_digest mismatch → exit 5
# ---------------------------------------------------------------------------


def test_verify_check3_digest_mismatch(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
    approver_key_path: Path,
    approver_cert_path: Path,
) -> None:
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    policy_json = _policy_json(["proposer"], ["approver"])
    version = policy_store.put(policy_json)

    proposal_env = _make_proposal_envelope(
        CAPSULE_ID, str(version), proposer_key_path, proposer_cert_path, "proposer"
    )
    proposal_uuid = bundle_store.put_proposal(CAPSULE_ID, proposal_env)

    time.sleep(0.05)

    # Build approval with wrong digest
    approval_pred = {
        "role": "approver",
        "proposal_digest": {"sha256": "b" * 64},  # wrong
        "decision": "approved",
        "approver_subject": "approver",
        "timestamp": "2099-01-01T00:00:01+00:00",
    }
    approval_env = sign_promote_envelope(
        json.dumps(approval_pred).encode(),
        APPROVAL_PAYLOAD_TYPE,
        approver_key_path,
        approver_cert_path,
    )
    bundle_store.put_approval(CAPSULE_ID, approval_env, proposal_uuid)

    result = verify_sod(CAPSULE_ID, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 5


# ---------------------------------------------------------------------------
# Check 4 failure: self-approval → exit 6
# ---------------------------------------------------------------------------


def test_verify_check4_self_approval(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
) -> None:
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    # Both proposer_key_ids and approver_key_ids include "proposer"
    policy_json = _policy_json(["proposer"], ["proposer"])
    version = policy_store.put(policy_json)

    proposal_env = _make_proposal_envelope(
        CAPSULE_ID, str(version), proposer_key_path, proposer_cert_path, "proposer"
    )
    proposal_uuid = bundle_store.put_proposal(CAPSULE_ID, proposal_env)

    time.sleep(0.05)

    # Approve with the SAME key (proposer signing the approval)
    approval_env = _make_approval_envelope(
        proposal_env, proposer_key_path, proposer_cert_path, "proposer"
    )
    bundle_store.put_approval(CAPSULE_ID, approval_env, proposal_uuid)

    result = verify_sod(CAPSULE_ID, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 6


# ---------------------------------------------------------------------------
# Check 5 failure: timestamp ordering → exit 7
# ---------------------------------------------------------------------------


def test_verify_check5_timestamp_ordering(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
    approver_key_path: Path,
    approver_cert_path: Path,
) -> None:
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    policy_json = _policy_json(["proposer"], ["approver"])
    version = policy_store.put(policy_json)

    proposal_env = _make_proposal_envelope(
        CAPSULE_ID, str(version), proposer_key_path, proposer_cert_path, "proposer"
    )
    proposal_uuid = bundle_store.put_proposal(CAPSULE_ID, proposal_env)

    # Build an approval with timestamp BEFORE the proposal
    canonical = jcs.canonicalize(json.loads(proposal_env))
    digest = hashlib.sha256(canonical).hexdigest()

    approval_pred = {
        "role": "approver",
        "proposal_digest": {"sha256": digest},
        "decision": "approved",
        "approver_subject": "approver",
        "timestamp": "2000-01-01T00:00:01+00:00",  # in the past
    }
    approval_env = sign_promote_envelope(
        json.dumps(approval_pred).encode(),
        APPROVAL_PAYLOAD_TYPE,
        approver_key_path,
        approver_cert_path,
    )
    bundle_store.put_approval(CAPSULE_ID, approval_env, proposal_uuid)

    result = verify_sod(CAPSULE_ID, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 7


# ---------------------------------------------------------------------------
# Missing approval → exit 8
# ---------------------------------------------------------------------------


def test_verify_missing_approval(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
) -> None:
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    policy_json = _policy_json(["proposer"], ["approver"])
    version = policy_store.put(policy_json)

    proposal_env = _make_proposal_envelope(
        CAPSULE_ID, str(version), proposer_key_path, proposer_cert_path, "proposer"
    )
    bundle_store.put_proposal(CAPSULE_ID, proposal_env)
    # No approval added

    result = verify_sod(CAPSULE_ID, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 8


# ---------------------------------------------------------------------------
# Missing proposal → exit 9
# ---------------------------------------------------------------------------


def test_verify_missing_proposal(tmp_path: Path) -> None:
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    result = verify_sod("cap-no-proposal", policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 9


# ---------------------------------------------------------------------------
# Garbage envelope → proposal sig invalid (exit 3)
# ---------------------------------------------------------------------------


def test_verify_garbage_proposal_envelope_exits_3(tmp_path: Path) -> None:
    """Proposal file exists but has garbage envelope — EnvelopeError → exit 3."""
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    capsule_id = "cap-garbage-proposal"
    proposal_dir = tmp_path / "promote" / capsule_id / "proposal"
    proposal_dir.mkdir(parents=True)
    import json as _json
    (proposal_dir / "garbage-uuid.json").write_text(
        _json.dumps({"envelope": "this is not a valid DSSE envelope"}),
        encoding="utf-8",
    )

    result = verify_sod(capsule_id, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# Policy version not found → exit 3
# ---------------------------------------------------------------------------


def test_verify_unknown_policy_version_exits_3(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
) -> None:
    """Valid proposal referencing non-existent policy version → exit 3."""
    import json as _json

    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    capsule_id = "cap-bad-policy-ver"
    proposal_pred = build_proposal_predicate(
        capsule_id=capsule_id,
        capsule_digest_hex="b" * 64,
        target_env="staging",
        justification="Justification long enough to pass schema validation.",
        proposer_subject="proposer",
        policy_version="999",
    )
    payload = _json.dumps(proposal_pred).encode()
    envelope = sign_promote_envelope(
        payload, PROPOSAL_PAYLOAD_TYPE, proposer_key_path, proposer_cert_path
    )
    bundle_store.put_proposal(capsule_id, envelope)

    result = verify_sod(capsule_id, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 3


# ---------------------------------------------------------------------------
# Garbage approval envelope → approval sig invalid (exit 4)
# ---------------------------------------------------------------------------


def test_verify_garbage_approval_envelope_exits_4(
    tmp_path: Path,
    proposer_key_path: Path,
    proposer_cert_path: Path,
) -> None:
    """Valid proposal + garbage approval bytes → approval sig invalid (exit 4)."""
    import json as _json

    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)

    policy_json = _policy_json(["proposer"], ["approver"])
    policy_version = policy_store.put(policy_json)

    capsule_id = "cap-garbage-approval"
    proposal_pred = build_proposal_predicate(
        capsule_id=capsule_id,
        capsule_digest_hex="c" * 64,
        target_env="staging",
        justification="Justification long enough to pass schema validation.",
        proposer_subject="proposer",
        policy_version=str(policy_version),
    )
    payload = _json.dumps(proposal_pred).encode()
    envelope = sign_promote_envelope(
        payload, PROPOSAL_PAYLOAD_TYPE, proposer_key_path, proposer_cert_path
    )
    proposal_uuid = bundle_store.put_proposal(capsule_id, envelope)

    approval_dir = tmp_path / "promote" / capsule_id / "approval"
    approval_dir.mkdir(parents=True)
    (approval_dir / f"{proposal_uuid}.json").write_text(
        _json.dumps({"envelope": "garbage approval", "approval_uuid": "x"}),
        encoding="utf-8",
    )

    result = verify_sod(capsule_id, policy_store, bundle_store)
    assert result.passed is False
    assert result.exit_code == 4
