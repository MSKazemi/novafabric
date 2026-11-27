"""``POST /api/runs/{id}/verify`` reports every check ``nova verify`` runs, one by one.

Issue #271 (the dashboard's per-check verification list): the route returned the
three NovaSeal booleans and one folded ``capsule_binding_ok``, so the dashboard could
not tell "the evidence binding passed" from "the seal has no ``evidence_digests``, so
the binding was never checked", nor show the capsule-id, chain, capture-tampering and
special-file checks the CLI prints. The additive fields below carry them, under the
names ``nova verify --json`` uses where it has one, and agree with the CLI's values.
"""

from __future__ import annotations

import datetime
import json
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

TOKEN = "test-token-run-verify-per-check"
HOST = {"host": "127.0.0.1:4321"}
RUN_ID = "01HRUNVERIFYPERCHECK000000"

#: Fields the route and ``nova verify --json`` both report, which must agree.
SHARED_WITH_CLI = (
    "valid",
    "signature_ok",
    "timestamp_ok",
    "timestamp_trust",
    "timestamp_present",
    "timestamp_strict",
    "log_integrity_ok",
    "log_inclusion",
    "log_notes",
    "sealer_log",
    "unanchored_claims",
    "capsule_binding_ok",
    "capsule_id_ok",
    "manifest_file",
    "unsealed_manifests",
    "unsealed_refs",
    "binding_errors",
    "ca_chain_checked",
    "ca_chain_ok",
    "tsa_chain_checked",
    "tsa_chain_ok",
    "identity_trust",
    "identity_statement",
    "signer_subject",
    "local_seal_identity",
    "pae_encoding",
    "signing_intent",
    "capture_tampered",
    "special_entries",
)


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
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "run-verify-per-check")])
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


def _seal(capsule: Path, manifest: dict[str, object], tmp_path: Path) -> None:
    key_path, cert_path = _identity(tmp_path)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(tmp_path / "sealer" / "merkle.db"),
    )
    bundle = seal.seal(manifest)
    seal.__dict__["_merkle"].close()
    seal_dir = capsule / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry))


def _capsule(tmp_path: Path, *, with_digests: bool = True) -> Path:
    capsule = tmp_path / "runs" / RUN_ID
    (capsule / "outputs").mkdir(parents=True)
    (capsule / "outputs" / "stdout.txt").write_text("hello\n")
    manifest: dict[str, object] = {"run_id": RUN_ID, "status": "success"}
    if with_digests:
        manifest["evidence_digests"] = evidence_digests(capsule)
    _seal(capsule, manifest, tmp_path)
    return capsule


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    monkeypatch.delenv("NOVAFABRIC_SEAL_DB_PATH", raising=False)
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)


def _route(tmp_path: Path) -> dict[str, object]:
    client = TestClient(
        create_app(token=TOKEN, capsule_dir=tmp_path / "runs", db_path=tmp_path / "r.db"),
        raise_server_exceptions=False,
    )
    response = client.post(f"/api/runs/{RUN_ID}/verify", params={"token": TOKEN}, headers=HOST)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _cli(capsule: Path) -> tuple[int, dict[str, object]]:
    result = CliRunner().invoke(cli_app, ["verify", "--json", str(capsule)])
    assert isinstance(result.exception, (SystemExit, type(None))), result.output
    return result.exit_code, json.loads(result.stdout)


def _assert_agrees_with_cli(body: dict[str, object], cli: dict[str, object]) -> None:
    for key in SHARED_WITH_CLI:
        assert key in body, f"route does not report {key}"
        assert body[key] == cli[key], f"{key}: route {body[key]!r} != CLI {cli[key]!r}"


def test_a_sealed_capsule_reports_every_check_as_the_cli_does(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    body = _route(tmp_path)
    code, cli = _cli(capsule)
    _assert_agrees_with_cli(body, cli)
    assert code == 0
    assert body["valid"] is True
    assert body["manifest_present"] is True
    assert body["manifest_ok"] is True
    assert body["evidence_digests_present"] is True
    assert body["evidence_digests_ok"] is True
    assert body["capsule_id_ok"] is True
    # No novaseal.yaml here: no chain was checked, so none is reported as passed.
    assert body["ca_chain_checked"] is False
    assert body["ca_chain_ok"] is None
    assert body["ca_chain_reason"] is None
    assert body["tsa_chain_checked"] is False
    assert body["tsa_chain_ok"] is None
    assert body["identity_trust"] == "self-asserted"
    assert body["timestamp_present"] is False
    assert body["capture_tampered"] is None
    assert body["special_entries"] == []


def test_a_seal_without_evidence_digests_reports_the_binding_as_not_checked(
    tmp_path: Path,
) -> None:
    """Sealed before ``evidence_digests``: the folded ``capsule_binding_ok`` is True,
    but the evidence half never ran — the dashboard must be able to say so."""
    capsule = _capsule(tmp_path, with_digests=False)
    body = _route(tmp_path)
    code, cli = _cli(capsule)
    _assert_agrees_with_cli(body, cli)
    assert body["evidence_digests_present"] is False
    assert body["evidence_digests_ok"] is False
    assert body["capsule_binding_ok"] is True
    assert body["valid"] is (code == 0)


def test_an_evidence_file_edited_after_sealing_names_the_file(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "outputs" / "stdout.txt").write_text("edited\n")
    body = _route(tmp_path)
    code, cli = _cli(capsule)
    _assert_agrees_with_cli(body, cli)
    assert code == 1
    assert body["valid"] is False
    assert body["evidence_digests_present"] is True
    assert body["evidence_digests_ok"] is False
    binding_errors = body["binding_errors"]
    assert isinstance(binding_errors, list)
    assert any("outputs/stdout.txt" in str(e) for e in binding_errors), binding_errors


def test_a_planted_link_is_reported_as_a_special_entry(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "outputs" / "planted").symlink_to("stdout.txt")
    body = _route(tmp_path)
    code, cli = _cli(capsule)
    _assert_agrees_with_cli(body, cli)
    assert code == 1
    assert body["valid"] is False
    special = body["special_entries"]
    assert isinstance(special, list) and special
    assert any("planted" in str(e) for e in special), special


def test_an_unsealed_capsule_reports_no_per_check_fields(tmp_path: Path) -> None:
    """Not sealed: nothing ran, so no check field may appear to have passed."""
    capsule = tmp_path / "runs" / RUN_ID
    capsule.mkdir(parents=True)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump({"run_id": RUN_ID}))
    body = _route(tmp_path)
    assert body["sealed"] is False
    for key in ("signature_ok", "capsule_id_ok", "evidence_digests_ok", "special_entries"):
        assert key not in body
