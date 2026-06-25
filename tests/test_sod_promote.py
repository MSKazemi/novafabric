"""Tests for D-5 maker-checker dual-approval (ADR-0058)."""
from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.audit._models import AuditEventType
from novafabric.registry.service import (
    AssetNotFoundError,
    SoDViolationError,
    approve_promotion,
    promote_asset,
    propose_promotion,
    register_asset,
)
from novafabric.registry.store import get_connection, init_schema  # noqa: F401
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec
from novafabric.trust.keyring import (
    canonical_payload,
    ensure_keypair,
    sign_payload,
    verify_sig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _register_model(tmp_db: Path) -> None:
    spec = validate_spec(FIXTURES_DIR / "valid_model.yaml")
    register_asset(spec, FIXTURES_DIR / "valid_model.yaml", db_path=tmp_db)


def _promote_to_validated(tmp_db: Path) -> None:
    promote_asset("fraud-model", "1.0.0", AssetStatus.validated, actor="ci", db_path=tmp_db)


# ---------------------------------------------------------------------------
# Keyring unit tests
# ---------------------------------------------------------------------------


def test_ensure_keypair_creates_key(tmp_path: Path) -> None:
    import novafabric.trust.keyring as kr
    original_dir = kr._KEYRING_DIR
    kr._KEYRING_DIR = tmp_path / "keyring"
    try:
        key, fp = ensure_keypair("alice@host")
        assert len(fp) == 16
        assert (kr._KEYRING_DIR / "alice_at_host.pem").exists()
    finally:
        kr._KEYRING_DIR = original_dir


def test_ensure_keypair_idempotent(tmp_path: Path) -> None:
    import novafabric.trust.keyring as kr
    original_dir = kr._KEYRING_DIR
    kr._KEYRING_DIR = tmp_path / "keyring"
    try:
        _, fp1 = ensure_keypair("alice@host")
        _, fp2 = ensure_keypair("alice@host")
        assert fp1 == fp2
    finally:
        kr._KEYRING_DIR = original_dir


def test_distinct_identities_have_distinct_fingerprints(tmp_path: Path) -> None:
    import novafabric.trust.keyring as kr
    original_dir = kr._KEYRING_DIR
    kr._KEYRING_DIR = tmp_path / "keyring"
    try:
        _, fp_alice = ensure_keypair("alice@host")
        _, fp_bob = ensure_keypair("bob@host")
        assert fp_alice != fp_bob
    finally:
        kr._KEYRING_DIR = original_dir


def test_sign_and_verify(tmp_path: Path) -> None:
    import novafabric.trust.keyring as kr
    original_dir = kr._KEYRING_DIR
    kr._KEYRING_DIR = tmp_path / "keyring"
    try:
        private_key, _ = ensure_keypair("alice@host")
        payload = b"test payload"
        sig = sign_payload(private_key, payload)
        assert verify_sig(private_key.public_key(), sig, payload)
        assert not verify_sig(private_key.public_key(), sig, b"tampered")
    finally:
        kr._KEYRING_DIR = original_dir


# ---------------------------------------------------------------------------
# propose_promotion service tests
# ---------------------------------------------------------------------------


def test_propose_promotion_happy_path(tmp_path: Path) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    result = propose_promotion(
        name="fraud-model",
        version="1.0.0",
        to_status=AssetStatus.staging,
        proposer="alice",
        proposer_key_fp="aabbcc001122",
        proposer_sig="fakesig",
        db_path=tmp_db,
    )
    assert result["state"] == "open"
    assert result["to_status"] == "staging"
    assert result["proposer"] == "alice"
    assert len(result["proposal_id"]) == 36  # UUID


def test_propose_promotion_asset_not_found(tmp_path: Path) -> None:
    tmp_db = tmp_path / "test.db"
    conn = get_connection(tmp_db)
    init_schema(conn)
    conn.close()
    with pytest.raises(AssetNotFoundError):
        propose_promotion(
            "ghost", "1.0.0", AssetStatus.staging, "alice", "fp", "sig", db_path=tmp_db
        )


def test_propose_promotion_invalid_transition(tmp_path: Path) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    # development → production is valid; development → archived is valid;
    # validated → development is NOT valid.
    _promote_to_validated(tmp_db)
    with pytest.raises(Exception):
        propose_promotion(
            "fraud-model", "1.0.0", AssetStatus.development,
            "alice", "fp", "sig", db_path=tmp_db
        )


def test_propose_promotion_blocks_duplicate_open(tmp_path: Path) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)
    propose_promotion(
        "fraud-model", "1.0.0", AssetStatus.staging,
        "alice", "fp_a", "sig_a", db_path=tmp_db
    )
    with pytest.raises(SoDViolationError, match="open proposal"):
        propose_promotion(
            "fraud-model", "1.0.0", AssetStatus.staging,
            "alice", "fp_a", "sig_a2", db_path=tmp_db
        )


# ---------------------------------------------------------------------------
# approve_promotion service tests
# ---------------------------------------------------------------------------


def test_approve_promotion_happy_path(tmp_path: Path) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    propose_promotion(
        "fraud-model", "1.0.0", AssetStatus.staging,
        "alice", "fp_alice", "sig_alice", db_path=tmp_db
    )
    result = approve_promotion(
        "fraud-model", "1.0.0",
        approver="bob",
        approver_key_fp="fp_bob",
        approver_sig="sig_bob",
        db_path=tmp_db,
    )
    assert result["status"] == "staging"
    assert result["approver"] == "bob"
    assert "proposal_id" in result


def test_approve_promotion_same_key_blocked(tmp_path: Path) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    propose_promotion(
        "fraud-model", "1.0.0", AssetStatus.staging,
        "alice", "fp_shared", "sig_alice", db_path=tmp_db
    )
    with pytest.raises(SoDViolationError, match="distinct keypairs"):
        approve_promotion(
            "fraud-model", "1.0.0",
            approver="bob",
            approver_key_fp="fp_shared",  # same fingerprint — SoD violation
            approver_sig="sig_bob",
            db_path=tmp_db,
        )


def test_approve_promotion_same_identity_blocked(tmp_path: Path) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    propose_promotion(
        "fraud-model", "1.0.0", AssetStatus.staging,
        "alice", "fp_alice", "sig_alice", db_path=tmp_db
    )
    with pytest.raises(SoDViolationError, match="different identities"):
        approve_promotion(
            "fraud-model", "1.0.0",
            approver="alice",  # same identity — SoD violation
            approver_key_fp="fp_alice2",
            approver_sig="sig_alice2",
            db_path=tmp_db,
        )


def test_approve_promotion_no_open_proposal(tmp_path: Path) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    with pytest.raises(AssetNotFoundError, match="No open promotion proposal"):
        approve_promotion(
            "fraud-model", "1.0.0",
            approver="bob",
            approver_key_fp="fp_bob",
            approver_sig="sig_bob",
            db_path=tmp_db,
        )


# ---------------------------------------------------------------------------
# Audit trail tests
# ---------------------------------------------------------------------------


def test_audit_entries_written(tmp_path: Path) -> None:
    import json

    import novafabric.audit as audit_module

    original_path = audit_module.AUDIT_LOG_PATH
    audit_log_path = tmp_path / "audit.jsonl"
    audit_module.AUDIT_LOG_PATH = audit_log_path

    # patch the service module too
    import novafabric.registry.service as svc
    svc.AUDIT_LOG_PATH = audit_log_path

    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    try:
        propose_promotion(
            "fraud-model", "1.0.0", AssetStatus.staging,
            "alice", "fp_alice", "sig_alice", db_path=tmp_db
        )
        approve_promotion(
            "fraud-model", "1.0.0",
            approver="bob",
            approver_key_fp="fp_bob",
            approver_sig="sig_bob",
            db_path=tmp_db,
        )
    finally:
        audit_module.AUDIT_LOG_PATH = original_path
        svc.AUDIT_LOG_PATH = original_path

    lines = [json.loads(line) for line in audit_log_path.read_text().splitlines() if line.strip()]
    event_types = [entry["event_type"] for entry in lines]
    assert AuditEventType.PROMOTE_PROPOSE in event_types
    assert AuditEventType.PROMOTE_APPROVE in event_types


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def test_cli_promote_propose_and_approve(tmp_path: Path) -> None:
    import novafabric.trust.keyring as kr

    original_keyring = kr._KEYRING_DIR
    kr._KEYRING_DIR = tmp_path / "keyring"

    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    try:
        # Propose
        alice_key, alice_fp = ensure_keypair("alice")
        payload_a = canonical_payload("pending|fraud-model@1.0.0:staging", "fraud-model", "1.0.0", "staging")
        sig_a = sign_payload(alice_key, payload_a)
        prop = propose_promotion(
            "fraud-model", "1.0.0", AssetStatus.staging,
            "alice", alice_fp, sig_a, db_path=tmp_db
        )
        assert prop["state"] == "open"

        # Approve with a different identity
        bob_key, bob_fp = ensure_keypair("bob")
        payload_b = canonical_payload("pending|fraud-model@1.0.0:approve", "fraud-model", "1.0.0", "approve")
        sig_b = sign_payload(bob_key, payload_b)
        result = approve_promotion(
            "fraud-model", "1.0.0",
            approver="bob",
            approver_key_fp=bob_fp,
            approver_sig=sig_b,
            db_path=tmp_db,
        )
        assert result["status"] == "staging"
        assert alice_fp != bob_fp
    finally:
        kr._KEYRING_DIR = original_keyring
