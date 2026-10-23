"""Serve's verify routes verify the way ``nova verify`` does — and create nothing.

* ``POST /api/evidence/{id}/verify`` built ``NovaSeal(...)`` to check the local
  capsule's log inclusion. The constructor opens the Merkle log for *writing*, so
  a verification request created ``merkle_db`` (and its parent directories) when
  it did not exist. Verification must never create a log (ADR-0297 S2): an empty
  one proves nothing.
* ``POST /api/runs/{id}/verify`` answered ``configured: false`` and verified
  nothing when no ``novaseal.yaml`` exists, while ``nova verify`` and the trust
  radar verify a sealed capsule from its own files (``verify_capsule(dir, None)``).
"""

from __future__ import annotations

import datetime
import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests
from novafabric.cli.main import app as cli_app
from novafabric.serve.app import create_app
from novafabric.trust.novaseal import KeyConfig, NovaSeal

TOKEN = "test-token-verify-creates-nothing"
HOST = {"host": "127.0.0.1:4321"}
RUN_ID = "01HVERIFYCREATESNOTHING000"


def _identity(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "verify-creates-nothing")])
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
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _sealed_capsule(root: Path, key_path: Path, cert_path: Path, sealer_db: Path) -> Path:
    capsule = root / RUN_ID
    (capsule / "outputs").mkdir(parents=True)
    (capsule / "outputs" / "stdout.txt").write_text("hello\n")
    manifest: dict[str, object] = {"run_id": RUN_ID, "status": "success"}
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(sealer_db),
    )
    bundle = seal.seal(manifest)
    seal.__dict__["_merkle"].close()
    seal_dir = capsule / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry))
    return capsule


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    monkeypatch.delenv("NOVAFABRIC_SEAL_DB_PATH", raising=False)
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setenv("NOVAFABRIC_EVIDENCE_DIR", str(evidence))
    key_path, cert_path = _identity(tmp_path)
    capsules = tmp_path / "runs"
    capsules.mkdir()
    capsule = _sealed_capsule(capsules, key_path, cert_path, tmp_path / "sealer" / "merkle.db")
    return {
        "home": home,
        "evidence": evidence,
        "capsules": capsules,
        "capsule": capsule,
        "key": key_path,
        "cert": cert_path,
        "tmp": tmp_path,
    }


def _client(env: dict[str, Path]) -> TestClient:
    return TestClient(
        create_app(token=TOKEN, capsule_dir=env["capsules"], db_path=env["tmp"] / "r.db"),
        raise_server_exceptions=False,
    )


def _profile_with_absent_log(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> Path:
    """A novaseal.yaml whose merkle_db does not exist (a verifier's machine)."""
    absent = env["tmp"] / "never" / "created" / "merkle.db"
    config = env["tmp"] / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {env['key']}\ncert_path: {env['cert']}\n"
        f"tsa_url: \nmerkle_db: {absent}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    return absent


def _bundle_for_run(evidence: Path, bundle_id: str) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"subject": {"run_id": RUN_ID}}))
    (evidence / f"{bundle_id}.zip").write_bytes(buf.getvalue())


# ── evidence verify route ──────────────────────────────────────────────────


def test_evidence_verify_never_creates_the_merkle_log(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = _profile_with_absent_log(env, monkeypatch)
    _bundle_for_run(env["evidence"], "b1")
    r = _client(env).post("/api/evidence/b1/verify", params={"token": TOKEN}, headers=HOST)
    assert r.status_code == 200, r.text
    assert not absent.exists(), "verification created a Merkle log"
    assert not absent.parent.exists(), "verification created the log's directory"
    body = r.json()
    assert body["seal_available"] is True
    # The local capsule carries an inclusion proof, so the check still runs.
    assert body["log_integrity_ok"] is True


def test_evidence_verify_reports_a_tampered_local_log_entry(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile_with_absent_log(env, monkeypatch)
    _bundle_for_run(env["evidence"], "b2")
    entry_file = env["capsule"] / ".seal" / "log-entry.json"
    entry = json.loads(entry_file.read_text())
    entry["entry"]["capsule_id"] = "0" * 64
    entry_file.write_text(json.dumps(entry))
    body = _client(env).post(
        "/api/evidence/b2/verify", params={"token": TOKEN}, headers=HOST
    ).json()
    assert body["log_integrity_ok"] is False
    assert body["valid"] is False


# ── runs verify route ──────────────────────────────────────────────────────


def test_run_verify_without_profile_verifies_from_the_capsule(env: dict[str, Path]) -> None:
    r = _client(env).post(f"/api/runs/{RUN_ID}/verify", params={"token": TOKEN}, headers=HOST)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sealed"] is True
    assert body["configured"] is False  # additive meaning: no novaseal.yaml here
    assert body["signature_ok"] is True
    assert body["capsule_binding_ok"] is True
    assert body["valid"] is True
    assert body["log_inclusion"] == "carried-proof"
    # Same verdict as `nova verify <capsule> --json` on the same machine.
    cli = CliRunner().invoke(cli_app, ["verify", str(env["capsule"]), "--json"])
    assert cli.exit_code == 0, cli.output
    assert json.loads(cli.stdout)["valid"] is True


def test_run_verify_without_profile_fails_a_tampered_capsule(env: dict[str, Path]) -> None:
    manifest = yaml.safe_load((env["capsule"] / "capsule.yaml").read_text())
    manifest["status"] = "success-FORGED"
    (env["capsule"] / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    body = _client(env).post(
        f"/api/runs/{RUN_ID}/verify", params={"token": TOKEN}, headers=HOST
    ).json()
    assert body["valid"] is False
    assert body["capsule_binding_ok"] is False
    cli = CliRunner().invoke(cli_app, ["verify", str(env["capsule"]), "--json"])
    assert cli.exit_code == 1


def test_run_verify_with_a_broken_profile_fails_like_the_cli(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable novaseal.yaml is a failed verification, never 'not configured'."""
    broken = env["tmp"] / "broken.yaml"
    broken.write_text("profile: local\n")  # missing cert_path / key_path
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(broken))
    body = _client(env).post(
        f"/api/runs/{RUN_ID}/verify", params={"token": TOKEN}, headers=HOST
    ).json()
    assert body["valid"] is False
    assert body["configured"] is True
    assert any("NovaSeal config" in e for e in body["errors"])
    cli = CliRunner().invoke(cli_app, ["verify", str(env["capsule"]), "--json"])
    assert cli.exit_code == 1


def test_run_verify_with_profile_creates_no_log(
    env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    absent = _profile_with_absent_log(env, monkeypatch)
    body = _client(env).post(
        f"/api/runs/{RUN_ID}/verify", params={"token": TOKEN}, headers=HOST
    ).json()
    assert body["configured"] is True
    assert body["valid"] is True
    assert not absent.parent.exists()
