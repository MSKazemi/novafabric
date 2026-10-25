"""Tests for D-5 maker-checker dual-approval (ADR-0058), as verified since ADR-0312.

Until ADR-0312 the service stored whatever signature and fingerprint it was handed
(these tests passed ``"fakesig"``). Every proposal and approval is now verified
against an operator-held signer trust list, so the service tests below sign for real.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.audit._models import AuditEventType
from novafabric.registry.service import (
    AssetNotFoundError,
    SelfApprovalError,
    SoDViolationError,
    approve_promotion,
    asset_spec_sha256,
    get_asset,
    get_open_proposal,
    new_proposal_id,
    promote_asset,
    propose_promotion,
    register_asset,
)
from novafabric.registry.signers import (
    SignerTrustList,
    approval_payload,
    parse_signer_trust_list,
    proposal_payload,
)
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec
from novafabric.trust.keyring import (
    ensure_keypair,
    public_key_b64,
    sign_payload,
    verify_sig,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"
NAME, VERSION = "fraud-model", "1.0.0"


def _register_model(tmp_db: Path) -> None:
    spec = validate_spec(FIXTURES_DIR / "valid_model.yaml")
    register_asset(spec, FIXTURES_DIR / "valid_model.yaml", db_path=tmp_db)


def _promote_to_validated(tmp_db: Path) -> None:
    promote_asset(NAME, VERSION, AssetStatus.validated, actor="ci", db_path=tmp_db)


@pytest.fixture
def keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import novafabric.trust.keyring as kr

    path = tmp_path / "keyring"
    monkeypatch.setattr(kr, "_KEYRING_DIR", path)
    return path


def _trust(bindings: dict[str, list[str]]) -> SignerTrustList:
    """Trust list binding each identity's keyring key to that identity and roles."""
    signers = []
    for identity, roles in bindings.items():
        key, _ = ensure_keypair(identity)
        signers.append(
            {"identity": identity, "public_key": public_key_b64(key.public_key()),
             "roles": roles}
        )
    return parse_signer_trust_list({"signers": signers}, source="test")


@pytest.fixture
def trust(keyring: Path) -> SignerTrustList:
    return _trust({"alice": ["proposer"], "bob": ["approver"]})


def _propose(
    tmp_db: Path, trust: SignerTrustList, who: str = "alice",
    to: AssetStatus = AssetStatus.staging,
) -> dict[str, object]:
    key, fp = ensure_keypair(who)
    pid = new_proposal_id()
    sig = sign_payload(
        key, proposal_payload(
            proposal_id=pid, name=NAME, version=VERSION, to_status=to.value,
            spec_sha256=asset_spec_sha256(NAME, VERSION, db_path=tmp_db),  # rlr-04
        )
    )
    return propose_promotion(
        NAME, VERSION, to, who, fp, sig, db_path=tmp_db, proposal_id=pid, trust_list=trust
    )


def _approve(tmp_db: Path, trust: SignerTrustList, who: str = "bob") -> dict[str, object]:
    key, fp = ensure_keypair(who)
    open_ = get_open_proposal(NAME, VERSION, db_path=tmp_db)
    assert open_ is not None
    sig = sign_payload(
        key,
        approval_payload(
            proposal_id=open_["proposal_id"], name=NAME, version=VERSION,
            to_status=open_["to_status"], proposer_sig=open_["proposer_sig"],
            spec_sha256=asset_spec_sha256(NAME, VERSION, db_path=tmp_db),
        ),
    )
    return approve_promotion(
        NAME, VERSION, approver=who, approver_key_fp=fp, approver_sig=sig,
        db_path=tmp_db, trust_list=trust,
    )


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
        assert not verify_sig(private_key.public_key(), "not base64 !!", payload)
    finally:
        kr._KEYRING_DIR = original_dir


# ---------------------------------------------------------------------------
# propose_promotion service tests
# ---------------------------------------------------------------------------


def test_propose_promotion_happy_path(tmp_path: Path, trust: SignerTrustList) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    result = _propose(tmp_db, trust)
    assert result["state"] == "open"
    assert result["to_status"] == "staging"
    assert result["proposer"] == "alice"
    assert result["verification"] == "verified"
    assert len(str(result["proposal_id"])) == 36  # UUID


def test_propose_promotion_asset_not_found(tmp_path: Path, trust: SignerTrustList) -> None:
    tmp_db = tmp_path / "test.db"
    conn = get_connection(tmp_db)
    init_schema(conn)
    conn.close()
    with pytest.raises(AssetNotFoundError):
        propose_promotion(
            "ghost", "1.0.0", AssetStatus.staging, "alice", "fp", "sig", db_path=tmp_db,
            proposal_id=new_proposal_id(), trust_list=trust,
        )


def test_propose_promotion_invalid_transition(tmp_path: Path, trust: SignerTrustList) -> None:
    from novafabric.registry.service import InvalidLifecycleTransitionError

    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    # validated → development is NOT a valid transition.
    _promote_to_validated(tmp_db)
    with pytest.raises(InvalidLifecycleTransitionError):
        _propose(tmp_db, trust, to=AssetStatus.development)


def test_propose_promotion_rejects_a_non_uuid_proposal_id(
    tmp_path: Path, trust: SignerTrustList
) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)
    with pytest.raises(ValueError, match="canonical UUID"):
        propose_promotion(
            NAME, VERSION, AssetStatus.staging, "alice", "fp", "sig", db_path=tmp_db,
            proposal_id="pending|fraud-model@1.0.0:staging", trust_list=trust,
        )


def test_propose_promotion_blocks_duplicate_open(tmp_path: Path, trust: SignerTrustList) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)
    _propose(tmp_db, trust)
    with pytest.raises(SoDViolationError, match="open proposal"):
        _propose(tmp_db, trust)


# ---------------------------------------------------------------------------
# approve_promotion service tests
# ---------------------------------------------------------------------------


def test_approve_promotion_happy_path(tmp_path: Path, trust: SignerTrustList) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    _propose(tmp_db, trust)
    result = _approve(tmp_db, trust)
    assert result["status"] == "staging"
    assert result["approver"] == "bob"
    assert result["verification"] == "verified"
    assert "proposal_id" in result


def test_approve_promotion_same_key_blocked(tmp_path: Path, keyring: Path) -> None:
    """One key holding both roles cannot countersign its own proposal."""
    trust = _trust({"alice": ["proposer", "approver"]})
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    _propose(tmp_db, trust)
    with pytest.raises(SelfApprovalError, match="distinct"):
        _approve(tmp_db, trust, who="alice")
    assert get_open_proposal(NAME, VERSION, db_path=tmp_db) is not None


def test_self_approval_is_still_a_sod_violation(tmp_path: Path, keyring: Path) -> None:
    """Existing ``except SoDViolationError`` handlers keep catching self-approval."""
    trust = _trust({"alice": ["proposer", "approver"]})
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    _propose(tmp_db, trust)
    with pytest.raises(SoDViolationError, match="SoD violation"):
        _approve(tmp_db, trust, who="alice")


def test_approve_promotion_no_open_proposal(tmp_path: Path, trust: SignerTrustList) -> None:
    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    with pytest.raises(AssetNotFoundError, match="No open promotion proposal"):
        approve_promotion(
            NAME, VERSION,
            approver="bob",
            approver_key_fp="fp_bob",
            approver_sig="sig_bob",
            db_path=tmp_db,
            trust_list=trust,
        )


# ---------------------------------------------------------------------------
# Audit trail tests
# ---------------------------------------------------------------------------


def test_audit_entries_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trust: SignerTrustList
) -> None:
    import json

    audit_log_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(audit_log_path))

    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)

    _propose(tmp_db, trust)
    _approve(tmp_db, trust)

    lines = [json.loads(line) for line in audit_log_path.read_text().splitlines() if line.strip()]
    event_types = [entry["event_type"] for entry in lines]
    assert AuditEventType.PROMOTE_PROPOSE in event_types
    assert AuditEventType.PROMOTE_APPROVE in event_types
    approve = next(e for e in lines if e["event_type"] == AuditEventType.PROMOTE_APPROVE)
    assert approve["actor"] == "bob"


# --- review replay-lineage-registry-04: the sign-off binds the reviewed content -------


def test_content_changed_after_the_proposal_cannot_be_approved(
    tmp_path: Path, trust: SignerTrustList
) -> None:
    """The maker signed the asset's spec digest; swapped content fails re-verification."""
    from novafabric.registry.signers import SignerVerificationError

    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)
    _propose(tmp_db, trust)
    conn = get_connection(tmp_db)
    conn.execute(
        "UPDATE assets SET spec_json = ? WHERE name = ? AND version = ?",
        ('{"description": "SWAPPED content nobody reviewed"}', NAME, VERSION),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SignerVerificationError, match="content changed"):
        _approve(tmp_db, trust)


def test_unregistering_supersedes_the_open_proposal(tmp_path: Path, trust: SignerTrustList) -> None:
    from novafabric.registry.service import unregister_asset

    tmp_db = tmp_path / "test.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)
    _propose(tmp_db, trust)
    unregister_asset(NAME, VERSION, actor="mallory", db_path=tmp_db)
    _register_model(tmp_db)  # different content may now be registered under this version
    assert get_open_proposal(NAME, VERSION, db_path=tmp_db) is None


def test_a_refused_promotion_leaves_the_proposal_open_and_audits_the_revert(
    tmp_path: Path, trust: SignerTrustList, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review replay-lineage-registry-17: the proposal was marked approved and the
    approval audited before the promote gates ran, so a refusal stranded it."""
    from unittest.mock import MagicMock, patch

    from novafabric.policy._models import PolicyDecision
    from novafabric.registry.service import PromotionBlockedError

    log = tmp_path / "revert-audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(log))
    tmp_db = tmp_path / "reg.db"
    _register_model(tmp_db)
    _promote_to_validated(tmp_db)
    _propose(tmp_db, trust)

    deny = MagicMock()
    deny.evaluate.return_value = PolicyDecision(allow=False, reason="not today", decision_id="d1")
    with patch("novafabric.registry.service.get_policy_engine", return_value=deny):
        with pytest.raises(PromotionBlockedError, match="not today"):
            _approve(tmp_db, trust)

    open_ = get_open_proposal(NAME, VERSION, db_path=tmp_db)
    assert open_ is not None and open_["state"] == "open"
    assert not open_.get("approver")
    assert get_asset(NAME, VERSION, db_path=tmp_db)["status"] == "validated"

    # Once the gate clears, the same proposal can be approved.
    allow = MagicMock()
    allow.evaluate.return_value = PolicyDecision(allow=True, reason="ok", decision_id="d2")
    with patch("novafabric.registry.service.get_policy_engine", return_value=allow):
        result = _approve(tmp_db, trust)
    assert result["status"] == "staging"

    import json as _json

    events = [_json.loads(line)["event_type"] for line in log.read_text().splitlines()]
    assert events.count("promote.approve_reverted") == 1
