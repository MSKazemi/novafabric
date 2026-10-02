"""`nova verify` must be self-contained: an independent auditor needs only the capsule.

Audit finding S2 (2026-10-01): the Merkle check called ``MerkleLog.verify_entry`` on the
verifier's *own* log database, so

* an auditor with no ``novaseal.yaml`` got "NovaSeal is not configured" and exit 1, and
* an auditor with a config but a fresh log got "Merkle inclusion proof failed" and exit 1,

for a capsule whose signature was perfectly valid. And on the sealer's machine the check
proved nothing about *this* capsule: it confirmed that whatever leaf sat at the recorded
``leaf_index`` was consistent with the log's root, never that the leaf was this capsule's.

These tests pin the fixed contract:

* new seals carry their inclusion proof in ``log-entry.json`` (additive field), so the
  proof verifies with no log at all;
* a capsule without a carried proof (sealed through v0.102.x) and without the sealer's log
  verifies with a visible "inclusion not checked" warning — not exit 1;
* when the sealer's log *is* present, the entry must be in it at the recorded index.
"""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "novaseal" / "legacy-v0.102"


@pytest.fixture()
def auditor_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh machine: no novaseal.yaml anywhere, empty NOVAFABRIC_HOME."""
    home = tmp_path / "auditor-home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    monkeypatch.setattr(
        "novafabric.trust.novaseal.config._default_config_path",
        lambda: home / "novaseal.yaml",
    )
    return home


@pytest.fixture()
def legacy_capsule(tmp_path: Path) -> Path:
    dst = tmp_path / "legacy-capsule"
    shutil.copytree(FIXTURES / "capsule", dst)
    return dst


def _sealer_log(path: Path) -> Path:
    """Rebuild the sealer's own Merkle log from the golden entries."""
    from novafabric.trust.novaseal.merkle import MerkleLog

    log = MerkleLog(path)
    for entry in json.loads((FIXTURES / "sealer-log-entries.json").read_text()):
        log.append(entry)
    log.close()
    return path


def _config(tmp_path: Path, merkle_db: Path) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "k.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "independent-verify")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "c.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: ''\nmerkle_db: {merkle_db}\n"
    )
    return config


def _new_sealed_capsule(tmp_path: Path) -> Path:
    """Seal a capsule with the current code (in a separate 'sealer' directory)."""
    from novafabric.trust.novaseal import KeyConfig, NovaSeal

    sealer = tmp_path / "sealer"
    sealer.mkdir()
    config = _config(sealer, sealer / "merkle.db")
    del config
    seal = NovaSeal(
        KeyConfig("local", str(sealer / "k.pem"), str(sealer / "c.pem")),
        tsa_url="",
        db_path=str(sealer / "merkle.db"),
    )
    for i in range(4):  # leaf 4 of 5: a non-trivial, odd-sized tree
        seal.seal({"run_id": f"other-{i}"})
    bundle = seal.seal({"run_id": "audited", "status": "success"})
    capsule = tmp_path / "new-capsule"
    (capsule / ".seal").mkdir(parents=True)
    (capsule / ".seal" / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (capsule / ".seal" / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (capsule / ".seal" / "log-entry.json").write_text(json.dumps(bundle.log_entry, indent=2))
    return capsule


# ---------------------------------------------------------------------------
# CLI — the auditor's view
# ---------------------------------------------------------------------------


def test_auditor_without_any_config_verifies_a_released_capsule(
    auditor_home: Path, legacy_capsule: Path
) -> None:
    result = runner.invoke(app, ["verify", str(legacy_capsule)])
    assert result.exit_code == 0, result.output
    assert "Signature (DSSE ECDSA P-256): OK" in result.output
    assert "inclusion not checked" in result.output.lower()


def test_auditor_with_own_config_and_fresh_log_verifies_a_released_capsule(
    tmp_path: Path, auditor_home: Path, legacy_capsule: Path
) -> None:
    config = _config(tmp_path, auditor_home / "fresh-merkle.db")
    result = runner.invoke(app, ["verify", str(legacy_capsule), "--seal-config", str(config)])
    assert result.exit_code == 0, result.output
    assert "inclusion not checked" in result.output.lower()


def test_sealer_log_confirms_a_released_capsule(
    tmp_path: Path, auditor_home: Path, legacy_capsule: Path
) -> None:
    config = _config(tmp_path, _sealer_log(tmp_path / "sealer.db"))
    result = runner.invoke(app, ["verify", str(legacy_capsule), "--seal-config", str(config)])
    assert result.exit_code == 0, result.output
    assert "Merkle log inclusion: OK" in result.output


def test_new_capsule_proves_inclusion_with_no_log_at_all(
    tmp_path: Path, auditor_home: Path
) -> None:
    capsule = _new_sealed_capsule(tmp_path)
    shutil.rmtree(tmp_path / "sealer")  # the sealer's log is gone
    result = runner.invoke(app, ["verify", str(capsule)])
    assert result.exit_code == 0, result.output
    assert "Merkle log inclusion: OK" in result.output
    assert "carried" in result.output.lower()


def test_a_forged_carried_proof_fails(tmp_path: Path, auditor_home: Path) -> None:
    capsule = _new_sealed_capsule(tmp_path)
    log_file = capsule / ".seal" / "log-entry.json"
    entry = json.loads(log_file.read_text())
    entry["inclusion_proof"][0] = "00" * 32
    log_file.write_text(json.dumps(entry))
    result = runner.invoke(app, ["verify", str(capsule)])
    assert result.exit_code == 1, result.output


def test_a_log_entry_for_another_capsule_fails(tmp_path: Path, auditor_home: Path) -> None:
    capsule = _new_sealed_capsule(tmp_path)
    log_file = capsule / ".seal" / "log-entry.json"
    entry = json.loads(log_file.read_text())
    entry["entry"]["capsule_id"] = "ab" * 32
    log_file.write_text(json.dumps(entry))
    result = runner.invoke(app, ["verify", str(capsule)])
    assert result.exit_code == 1, result.output


# ---------------------------------------------------------------------------
# Library — verify_seal_dir
# ---------------------------------------------------------------------------


def test_released_capsule_without_log_is_valid_but_not_inclusion_checked(
    legacy_capsule: Path,
) -> None:
    from novafabric.trust.novaseal import LOG_INCLUSION_NOT_CHECKED, verify_seal_dir

    result = verify_seal_dir(legacy_capsule / ".seal")
    assert result.valid, result.errors
    assert result.signature_ok
    assert result.log_inclusion == LOG_INCLUSION_NOT_CHECKED
    assert result.log_integrity_ok is False  # not checked is not "ok"


def test_released_capsule_against_the_sealer_log(tmp_path: Path, legacy_capsule: Path) -> None:
    from novafabric.trust.novaseal import LOG_INCLUSION_LOCAL, verify_seal_dir
    from novafabric.trust.novaseal.merkle import MerkleLog

    log = MerkleLog(_sealer_log(tmp_path / "sealer.db"))
    result = verify_seal_dir(legacy_capsule / ".seal", merkle_log=log)
    assert result.valid, result.errors
    assert result.log_inclusion == LOG_INCLUSION_LOCAL
    assert result.log_integrity_ok


def test_redirected_leaf_index_is_caught_by_the_sealer_log(
    tmp_path: Path, legacy_capsule: Path
) -> None:
    """Before the fix, pointing leaf_index at another capsule's leaf passed."""
    from novafabric.trust.novaseal import LOG_INCLUSION_FAILED, verify_seal_dir
    from novafabric.trust.novaseal.merkle import MerkleLog

    log_file = legacy_capsule / ".seal" / "log-entry.json"
    entry = json.loads(log_file.read_text())
    entry["leaf_index"] = 0
    log_file.write_text(json.dumps(entry))
    log = MerkleLog(_sealer_log(tmp_path / "sealer.db"))
    result = verify_seal_dir(legacy_capsule / ".seal", merkle_log=log)
    assert not result.valid
    assert result.log_inclusion == LOG_INCLUSION_FAILED


def test_new_seal_log_entry_carries_a_verifiable_inclusion_proof(tmp_path: Path) -> None:
    from novafabric.trust.novaseal.merkle import verify_inclusion_proof

    capsule = _new_sealed_capsule(tmp_path)
    entry = json.loads((capsule / ".seal" / "log-entry.json").read_text())
    assert entry["leaf_index"] == 4 and entry["tree_size"] == 5
    assert verify_inclusion_proof(
        entry["leaf_hash"],
        entry["leaf_index"],
        entry["inclusion_proof"],
        entry["root_hash"],
        entry["tree_size"],
    )


def test_truncated_carried_proof_fails(tmp_path: Path) -> None:
    from novafabric.trust.novaseal import LOG_INCLUSION_FAILED, verify_seal_dir

    capsule = _new_sealed_capsule(tmp_path)
    log_file = capsule / ".seal" / "log-entry.json"
    entry = json.loads(log_file.read_text())
    entry["inclusion_proof"] = entry["inclusion_proof"][:-1]
    log_file.write_text(json.dumps(entry))
    result = verify_seal_dir(capsule / ".seal")
    assert result.log_inclusion == LOG_INCLUSION_FAILED
    assert not result.valid
