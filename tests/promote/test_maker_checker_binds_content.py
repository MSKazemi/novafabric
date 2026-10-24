"""Maker-checker approval binds the capsule's content, not its id (review cli-b-01).

Proposals and bypasses signed ``sha256(capsule_id)`` — a hash of the id string — as
the "capsule digest", and nothing compared it with anything: ``nova seal verify``
passed for a capsule that did not exist, and an approval survived any later change
to the capsule. They now sign the SHA-256 of ``capsule.yaml``, and ``verify_sod``
recomputes it from the capsule it is given.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from _promote_capsule import make_capsule
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.capsule_digest import capsule_content_digest, id_stand_in_digest
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
CAPSULE = "cap-content-001"
runner = CliRunner()


def _approved(
    tmp_path: Path, digest: str, *, policy_db: Path | None = None, data_dir: Path | None = None
) -> tuple[PolicyStore, PromoteBundleStore]:
    policy_store = PolicyStore(policy_db or tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(data_dir or tmp_path)
    version = str(policy_store.put(json.dumps(build_policy_predicate(["proposer"], ["approver"]))))
    proposal = sign_promote_envelope(
        json.dumps(build_proposal_predicate(
            capsule_id=CAPSULE, capsule_digest_hex=digest, target_env="staging",
            justification="Ready for production after the full review cycle.",
            proposer_subject="proposer", policy_version=version,
        )).encode(),
        PROPOSAL_PAYLOAD_TYPE, FIXTURE_KEYS / "proposer.pem", FIXTURE_KEYS / "proposer_cert.pem",
    )
    proposal_uuid = bundle_store.put_proposal(CAPSULE, proposal)
    time.sleep(0.05)
    approval = sign_promote_envelope(
        json.dumps(build_approval_predicate(proposal, "approved", "approver")).encode(),
        APPROVAL_PAYLOAD_TYPE, FIXTURE_KEYS / "approver.pem", FIXTURE_KEYS / "approver_cert.pem",
    )
    bundle_store.put_approval(CAPSULE, approval, proposal_uuid)
    return policy_store, bundle_store


def test_an_approval_of_this_capsule_verifies_with_its_content(tmp_path: Path) -> None:
    capsule = make_capsule(CAPSULE, tmp_path / "caps")
    policy, bundles = _approved(tmp_path, capsule_content_digest(capsule))
    result = verify_sod(CAPSULE, policy, bundles, capsule_dir=capsule)
    assert result.passed, result.message
    assert result.content_checked is True


def test_a_capsule_changed_after_approval_fails(tmp_path: Path) -> None:
    capsule = make_capsule(CAPSULE, tmp_path / "caps")
    policy, bundles = _approved(tmp_path, capsule_content_digest(capsule))
    (capsule / "capsule.yaml").write_text(f"run_id: {CAPSULE}\nstatus: failure\n")
    result = verify_sod(CAPSULE, policy, bundles, capsule_dir=capsule)
    assert (result.passed, result.exit_code) == (False, 10)
    assert "changed since" in result.message


def test_a_capsule_that_does_not_exist_fails(tmp_path: Path) -> None:
    policy, bundles = _approved(tmp_path, "a" * 64)
    result = verify_sod(CAPSULE, policy, bundles, capsule_dir=tmp_path / "nowhere" / CAPSULE)
    assert (result.passed, result.exit_code) == (False, 10)


def test_a_proposal_that_signed_a_hash_of_the_id_must_be_re_proposed(tmp_path: Path) -> None:
    capsule = make_capsule(CAPSULE, tmp_path / "caps")
    policy, bundles = _approved(tmp_path, id_stand_in_digest(CAPSULE))
    result = verify_sod(CAPSULE, policy, bundles, capsule_dir=capsule)
    assert (result.passed, result.exit_code) == (False, 10)
    assert "re-propose" in result.message


def test_without_a_capsule_dir_the_result_says_content_was_not_checked(tmp_path: Path) -> None:
    policy, bundles = _approved(tmp_path, "a" * 64)
    result = verify_sod(CAPSULE, policy, bundles)
    assert result.passed
    assert result.content_checked is False


def _policy_db(tmp_path: Path) -> Path:
    db = tmp_path / "policy.db"
    res = runner.invoke(app, [
        "policy", "sign", "--key", str(FIXTURE_KEYS / "admin.pem"),
        "--cert", str(FIXTURE_KEYS / "admin_cert.pem"), "--proposer-subjects", "proposer",
        "--approver-subjects", "approver", "--db", str(db),
    ])
    assert res.exit_code == 0, res.output
    return db


def test_cli_propose_refuses_a_capsule_that_does_not_exist(tmp_path: Path) -> None:
    db, data = _policy_db(tmp_path), tmp_path / "data"
    res = runner.invoke(app, [
        "seal", "propose", "cap-does-not-exist",
        "--justification", "Ready for production after the full review cycle.",
        "--key", str(FIXTURE_KEYS / "proposer.pem"),
        "--cert", str(FIXTURE_KEYS / "proposer_cert.pem"),
        "--db", str(db), "--data-dir", str(data),
    ])
    assert res.exit_code == 1, res.output
    assert "nothing was proposed" in " ".join(res.output.split())
    assert PromoteBundleStore(data).list_proposals("cap-does-not-exist") == []


def test_cli_verify_of_a_capsule_that_does_not_exist_exits_10(tmp_path: Path) -> None:
    db = _policy_db(tmp_path)
    res = runner.invoke(app, [
        "seal", "verify", "cap-does-not-exist", "--db", str(db), "--data-dir", str(tmp_path),
    ])
    assert res.exit_code == 10, res.output


def test_cli_propose_then_verify_binds_and_catches_a_change(tmp_path: Path) -> None:
    capsule = make_capsule("cap-cli-bound")
    db, data = _policy_db(tmp_path), tmp_path / "data"
    propose = runner.invoke(app, [
        "seal", "propose", "cap-cli-bound",
        "--justification", "Ready for production after the full review cycle.",
        "--key", str(FIXTURE_KEYS / "proposer.pem"),
        "--cert", str(FIXTURE_KEYS / "proposer_cert.pem"),
        "--db", str(db), "--data-dir", str(data),
    ])
    assert propose.exit_code == 0, propose.output
    (uuid,) = PromoteBundleStore(data).list_proposals("cap-cli-bound")
    time.sleep(0.05)
    approve = runner.invoke(app, [
        "seal", "approve", uuid, "--capsule-id", "cap-cli-bound",
        "--key", str(FIXTURE_KEYS / "approver.pem"),
        "--cert", str(FIXTURE_KEYS / "approver_cert.pem"),
        "--db", str(db), "--data-dir", str(data),
    ], input="y\n")
    assert approve.exit_code == 0, approve.output
    verify = ["seal", "verify", "cap-cli-bound", "--db", str(db), "--data-dir", str(data)]
    assert runner.invoke(app, verify).exit_code == 0
    (capsule / "capsule.yaml").write_text("run_id: cap-cli-bound\nstatus: edited\n")
    assert runner.invoke(app, verify).exit_code == 10


def test_a_bypass_signed_for_other_content_does_not_skip_sod(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from novafabric.promote.predicates import BYPASS_PAYLOAD_TYPE, build_bypass_predicate

    capsule = make_capsule(CAPSULE, tmp_path / "caps")
    policy = PolicyStore(tmp_path / "merkle.db")
    policy.put(json.dumps(build_policy_predicate(["proposer"], ["approver"])))
    bundles = PromoteBundleStore(tmp_path)
    until = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

    def bypass(digest: str) -> None:
        predicate = build_bypass_predicate(
            CAPSULE, digest, "production", "x" * 60, "approver", until, []
        )
        bundles.put_bypass(CAPSULE, sign_promote_envelope(
            json.dumps(predicate).encode(), BYPASS_PAYLOAD_TYPE,
            FIXTURE_KEYS / "approver.pem", FIXTURE_KEYS / "approver_cert.pem",
        ), until)

    bypass(id_stand_in_digest(CAPSULE))
    stale = verify_sod(CAPSULE, policy, bundles, capsule_dir=capsule, require_bypass_audit=False)
    assert (stale.passed, stale.bypass_used) == (False, False)
    bypass(capsule_content_digest(capsule))
    bound = verify_sod(CAPSULE, policy, bundles, capsule_dir=capsule, require_bypass_audit=False)
    assert (bound.passed, bound.bypass_used, bound.content_checked) == (True, True, True)


def test_a_signed_rejection_is_not_an_approval(tmp_path: Path) -> None:
    """Review replay-lineage-registry-07: the verifier never read the approval's signed
    ``decision``, so an approver's signed "rejected" passed SoD."""
    capsule = make_capsule(CAPSULE, tmp_path / "caps")
    policy_store = PolicyStore(tmp_path / "merkle.db")
    bundle_store = PromoteBundleStore(tmp_path)
    version = str(policy_store.put(json.dumps(build_policy_predicate(["proposer"], ["approver"]))))
    proposal = sign_promote_envelope(
        json.dumps(build_proposal_predicate(
            capsule_id=CAPSULE, capsule_digest_hex=capsule_content_digest(capsule),
            target_env="staging", justification="Ready for production after review.",
            proposer_subject="proposer", policy_version=version,
        )).encode(),
        PROPOSAL_PAYLOAD_TYPE, FIXTURE_KEYS / "proposer.pem", FIXTURE_KEYS / "proposer_cert.pem",
    )
    proposal_uuid = bundle_store.put_proposal(CAPSULE, proposal)
    time.sleep(0.05)
    rejection = sign_promote_envelope(
        json.dumps(build_approval_predicate(proposal, "rejected", "approver")).encode(),
        APPROVAL_PAYLOAD_TYPE, FIXTURE_KEYS / "approver.pem", FIXTURE_KEYS / "approver_cert.pem",
    )
    bundle_store.put_approval(CAPSULE, rejection, proposal_uuid)
    result = verify_sod(CAPSULE, policy_store, bundle_store, capsule_dir=capsule)
    assert (result.passed, result.exit_code) == (False, 8)
    assert "rejected" in result.message


# --- server mode: POST /v0/seal/{capsule_id}/verify -----------------------------------
# The route called verify_sod without the capsule, so it answered passed: true for an
# approval of since-changed content, or of a capsule the server does not hold.


def _server_verify(tmp_path: Path, capsule_id: str = CAPSULE) -> dict[str, object]:
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.server.app import create_app
    from novafabric.server.config import ServerConfig

    app_ = create_app(ServerConfig(db_path=str(tmp_path / "server.db"), insecure_no_auth=True))
    response = TestClient(app_, raise_server_exceptions=False).post(f"/v0/seal/{capsule_id}/verify")
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def _approved_at_server_paths(tmp_path: Path, digest: str) -> None:
    from novafabric._paths import nova_home
    from novafabric.trust.novaseal.config import resolve_merkle_db_path

    policy, _ = _approved(
        tmp_path, digest, policy_db=resolve_merkle_db_path(), data_dir=nova_home()
    )
    policy.close()


def test_server_verify_binds_the_capsule_content(tmp_path: Path) -> None:
    capsule = make_capsule(CAPSULE)  # the server's capsule store
    _approved_at_server_paths(tmp_path, capsule_content_digest(capsule))
    body = _server_verify(tmp_path)
    assert (body["passed"], body["content_checked"]) == (True, True), body
    (capsule / "capsule.yaml").write_text(f"run_id: {CAPSULE}\nstatus: failure\n")
    body = _server_verify(tmp_path)
    assert (body["passed"], body["exit_code"]) == (False, 10), body


def test_server_verify_of_a_capsule_it_does_not_hold_fails(tmp_path: Path) -> None:
    _approved_at_server_paths(tmp_path, "a" * 64)
    body = _server_verify(tmp_path)
    assert (body["passed"], body["exit_code"]) == (False, 10), body
