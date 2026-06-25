"""CLI integration tests for nova seal propose/approve/verify and nova policy sign."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

FIXTURE_KEYS = Path(__file__).parent.parent / "fixtures" / "promote" / "keys"

runner = CliRunner()


def _proposer_key() -> Path:
    return FIXTURE_KEYS / "proposer.pem"


def _proposer_cert() -> Path:
    return FIXTURE_KEYS / "proposer_cert.pem"


def _approver_key() -> Path:
    return FIXTURE_KEYS / "approver.pem"


def _approver_cert() -> Path:
    return FIXTURE_KEYS / "approver_cert.pem"


def _admin_key() -> Path:
    return FIXTURE_KEYS / "admin.pem"


def _admin_cert() -> Path:
    return FIXTURE_KEYS / "admin_cert.pem"


@pytest.fixture
def setup_policy(tmp_path: Path) -> tuple[Path, Path, int]:
    """Create a policy signed by admin; return (db_path, data_dir, version)."""
    db_path = tmp_path / "merkle.db"
    result = runner.invoke(app, [
        "policy", "sign",
        "--key", str(_admin_key()),
        "--cert", str(_admin_cert()),
        "--proposer-subjects", "proposer",
        "--approver-subjects", "approver",
        "--bypass-valid-hours", "24",
        "--db", str(db_path),
    ])
    assert result.exit_code == 0, f"policy sign failed: {result.output}"
    # Extract version number from output
    for line in result.output.splitlines():
        if "version" in line.lower():
            parts = line.split()
            for part in parts:
                if part.isdigit():
                    return db_path, tmp_path, int(part)
    return db_path, tmp_path, 1


# ---------------------------------------------------------------------------
# nova policy sign
# ---------------------------------------------------------------------------


def test_policy_sign_success(tmp_path: Path) -> None:
    db_path = tmp_path / "merkle.db"
    result = runner.invoke(app, [
        "policy", "sign",
        "--key", str(_admin_key()),
        "--cert", str(_admin_cert()),
        "--proposer-subjects", "proposer,admin",
        "--approver-subjects", "approver",
        "--bypass-valid-hours", "48",
        "--db", str(db_path),
    ])
    assert result.exit_code == 0
    assert "version" in result.output.lower()


def test_policy_sign_bypass_hours_out_of_range(tmp_path: Path) -> None:
    db_path = tmp_path / "merkle.db"
    result = runner.invoke(app, [
        "policy", "sign",
        "--key", str(_admin_key()),
        "--cert", str(_admin_cert()),
        "--proposer-subjects", "proposer",
        "--approver-subjects", "approver",
        "--bypass-valid-hours", "200",
        "--db", str(db_path),
    ])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# nova seal propose
# ---------------------------------------------------------------------------


def test_seal_propose_happy_path(setup_policy: tuple[Path, Path, int]) -> None:
    db_path, data_dir, _ = setup_policy
    result = runner.invoke(app, [
        "seal", "propose", "cap-test-001",
        "--justification", "This capsule has passed all automated checks and peer review.",
        "--key", str(_proposer_key()),
        "--cert", str(_proposer_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"
    # Output should contain a UUID
    output = result.output
    assert any(len(line.strip()) == 36 or "Proposal created" in line for line in output.splitlines())


def test_seal_propose_short_justification(setup_policy: tuple[Path, Path, int]) -> None:
    db_path, data_dir, _ = setup_policy
    result = runner.invoke(app, [
        "seal", "propose", "cap-test-short",
        "--justification", "Too short",
        "--key", str(_proposer_key()),
        "--cert", str(_proposer_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code == 1
    # Error must mention justification and 20
    assert "justification" in result.output.lower()
    assert "20" in result.output
    # No proposal bundle should be written
    proposal_dir = data_dir / "promote" / "cap-test-short" / "proposal"
    assert not proposal_dir.exists() or list(proposal_dir.glob("*.json")) == []


def test_seal_propose_no_policy(tmp_path: Path) -> None:
    """Without a policy, propose should fail."""
    result = runner.invoke(app, [
        "seal", "propose", "cap-no-policy",
        "--justification", "This capsule has passed all automated checks.",
        "--key", str(_proposer_key()),
        "--cert", str(_proposer_cert()),
        "--db", str(tmp_path / "empty.db"),
        "--data-dir", str(tmp_path),
    ])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# nova seal approve
# ---------------------------------------------------------------------------


@pytest.fixture
def proposal_created(setup_policy: tuple[Path, Path, int]) -> tuple[Path, Path, str]:
    """Create a proposal; return (db_path, data_dir, proposal_uuid)."""
    db_path, data_dir, _ = setup_policy
    result = runner.invoke(app, [
        "seal", "propose", "cap-approve-test",
        "--justification", "This capsule has passed all automated checks and peer review.",
        "--key", str(_proposer_key()),
        "--cert", str(_proposer_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code == 0, f"propose failed: {result.output}"
    # Extract UUID from output
    proposal_uuid = None
    for line in result.output.splitlines():
        if "Proposal created:" in line:
            proposal_uuid = line.split(":", 1)[1].strip()
            break
    assert proposal_uuid is not None, f"Could not find UUID in output: {result.output}"
    return db_path, data_dir, proposal_uuid


def test_seal_approve_happy_path(proposal_created: tuple[Path, Path, str]) -> None:
    db_path, data_dir, proposal_uuid = proposal_created
    time.sleep(0.05)  # ensure timestamp ordering
    result = runner.invoke(app, [
        "seal", "approve", proposal_uuid,
        "--capsule-id", "cap-approve-test",
        "--key", str(_approver_key()),
        "--cert", str(_approver_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ], input="y\n")
    assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"
    assert "approval recorded" in result.output.lower()


def test_seal_approve_cancelled(proposal_created: tuple[Path, Path, str]) -> None:
    db_path, data_dir, proposal_uuid = proposal_created
    result = runner.invoke(app, [
        "seal", "approve", proposal_uuid,
        "--capsule-id", "cap-approve-test",
        "--key", str(_approver_key()),
        "--cert", str(_approver_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ], input="n\n")
    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()


# ---------------------------------------------------------------------------
# nova seal verify
# ---------------------------------------------------------------------------


@pytest.fixture
def full_bundle(proposal_created: tuple[Path, Path, str]) -> tuple[Path, Path]:
    """Create proposal + approval; return (db_path, data_dir)."""
    db_path, data_dir, proposal_uuid = proposal_created
    time.sleep(0.05)
    result = runner.invoke(app, [
        "seal", "approve", proposal_uuid,
        "--capsule-id", "cap-approve-test",
        "--key", str(_approver_key()),
        "--cert", str(_approver_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ], input="y\n")
    assert result.exit_code == 0, f"approve failed: {result.output}"
    return db_path, data_dir


def test_seal_verify_pass(full_bundle: tuple[Path, Path]) -> None:
    db_path, data_dir = full_bundle
    result = runner.invoke(app, [
        "seal", "verify", "cap-approve-test",
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code == 0, f"exit={result.exit_code}\n{result.output}"
    assert "passed" in result.output.lower()


def test_seal_verify_missing_approval(setup_policy: tuple[Path, Path, int]) -> None:
    db_path, data_dir, _ = setup_policy
    # Create proposal but no approval
    result = runner.invoke(app, [
        "seal", "propose", "cap-no-approval",
        "--justification", "This capsule has passed all automated checks and peer review.",
        "--key", str(_proposer_key()),
        "--cert", str(_proposer_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code == 0

    result = runner.invoke(app, [
        "seal", "verify", "cap-no-approval",
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code != 0


def test_seal_verify_self_approval(setup_policy: tuple[Path, Path, int]) -> None:
    """Proposer and approver are both 'proposer' — should fail with exit 6."""
    db_path, data_dir, _ = setup_policy

    # We need a policy that allows proposer to also approve
    # Re-sign policy with proposer in both lists
    result_sign = runner.invoke(app, [
        "policy", "sign",
        "--key", str(_admin_key()),
        "--cert", str(_admin_cert()),
        "--proposer-subjects", "proposer",
        "--approver-subjects", "proposer",  # same!
        "--db", str(db_path),
    ])
    assert result_sign.exit_code == 0

    # Create proposal
    result = runner.invoke(app, [
        "seal", "propose", "cap-self-approval",
        "--justification", "This capsule has passed all automated checks and peer review.",
        "--key", str(_proposer_key()),
        "--cert", str(_proposer_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code == 0

    proposal_uuid = None
    for line in result.output.splitlines():
        if "Proposal created:" in line:
            proposal_uuid = line.split(":", 1)[1].strip()
            break

    assert proposal_uuid is not None
    time.sleep(0.05)

    # Approve with same key as proposer
    result = runner.invoke(app, [
        "seal", "approve", proposal_uuid,
        "--capsule-id", "cap-self-approval",
        "--key", str(_proposer_key()),
        "--cert", str(_proposer_cert()),  # same cert!
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ], input="y\n")
    assert result.exit_code == 0  # approve command succeeds

    # Verify should fail with exit 6
    result = runner.invoke(app, [
        "seal", "verify", "cap-self-approval",
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code == 6


# ---------------------------------------------------------------------------
# nova policy test / policy explain edge cases
# ---------------------------------------------------------------------------


def test_policy_test_no_opa_binary(tmp_path: Path) -> None:
    """policy test command handles missing opa binary gracefully."""
    from unittest.mock import patch

    with patch("subprocess.run", side_effect=FileNotFoundError("opa")):
        result = runner.invoke(app, ["policy", "test"])
    assert result.exit_code == 1
    assert "opa" in result.output.lower()


def test_policy_test_with_opa_mock(tmp_path: Path) -> None:
    """policy test command propagates opa exit code."""
    from unittest.mock import MagicMock, patch

    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        result = runner.invoke(app, ["policy", "test"])
    assert result.exit_code == 0


def test_policy_explain_decision_not_found(tmp_path: Path) -> None:
    """policy explain exits 1 when decision ID is absent from audit log."""
    audit_log = tmp_path / "audit.jsonl"
    audit_log.write_text("", encoding="utf-8")
    result = runner.invoke(app, [
        "policy", "explain", "nonexistent-decision-id-xyz",
        "--audit-log", str(audit_log),
    ])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# nova policy sign — validation edge cases
# ---------------------------------------------------------------------------


def test_policy_sign_empty_proposers(tmp_path: Path) -> None:
    """policy sign rejects empty proposer list."""
    db_path = tmp_path / "merkle.db"
    result = runner.invoke(app, [
        "policy", "sign",
        "--key", str(_admin_key()),
        "--cert", str(_admin_cert()),
        "--proposer-subjects", ",",  # only commas → empty after strip
        "--approver-subjects", "approver",
        "--db", str(db_path),
    ])
    assert result.exit_code == 1
    assert "proposer" in result.output.lower()


def test_policy_sign_empty_approvers(tmp_path: Path) -> None:
    """policy sign rejects empty approver list."""
    db_path = tmp_path / "merkle.db"
    result = runner.invoke(app, [
        "policy", "sign",
        "--key", str(_admin_key()),
        "--cert", str(_admin_cert()),
        "--proposer-subjects", "proposer",
        "--approver-subjects", ",",  # only commas → empty after strip
        "--db", str(db_path),
    ])
    assert result.exit_code == 1
    assert "approver" in result.output.lower()


# ---------------------------------------------------------------------------
# nova seal approve — error edge cases
# ---------------------------------------------------------------------------


def test_seal_approve_proposal_not_found(setup_policy: tuple[Path, Path, int]) -> None:
    """approve exits 1 when the proposal UUID doesn't exist."""
    db_path, data_dir, _ = setup_policy
    result = runner.invoke(app, [
        "seal", "approve", "nonexistent-uuid-000",
        "--capsule-id", "cap-no-such-proposal",
        "--key", str(_approver_key()),
        "--cert", str(_approver_cert()),
        "--db", str(db_path),
        "--data-dir", str(data_dir),
    ])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()
