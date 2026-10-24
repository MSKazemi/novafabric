"""Audit seal evidence is a seal that verifies, not a file that exists (review cli-a-03).

The audit engine set ``evidence_seal_verified`` — and the evidence binding counted
``seal_signature`` satisfied — when a capsule held a file named ``seal-bundle.json``,
which nothing in the product writes; a genuinely sealed capsule (``.seal/``) never
counted. ``nova audit verify`` then printed ``Seals: verified`` from the report's own
field, and ``nova audit map`` exited 0 with every control missing although its help
promised exit 1.
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
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests
from novafabric.cli.audit import app as audit_app
from novafabric.compliance.audit.engine import AuditEngine
from novafabric.compliance.audit.loader import load_profile
from novafabric.evidence.binding import build_bindings
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.capsule_verify import capsule_seal_verifies

runner = CliRunner()


def _seal_key(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "audit-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / "seal.key", tmp_path / "seal.crt"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _capsule(root: Path, name: str) -> Path:
    capsule = root / name
    capsule.mkdir(parents=True)
    (capsule / "model-calls.jsonl").write_text('{"model":"m"}\n')
    (capsule / "capsule.yaml").write_text(yaml.safe_dump({"run_id": name, "status": "success"}))
    return capsule


def _seal(capsule: Path, tmp_path: Path) -> None:
    key_path, cert_path = _seal_key(tmp_path)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    bundle = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="", db_path=str(tmp_path / "merkle.db"),
    ).seal(manifest)
    seal_dir = capsule / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry))


def _scan(data_dir: Path) -> bool:
    return AuditEngine(data_dir, load_profile("iso42001")).scan().evidence_seal_verified


def test_a_file_named_seal_bundle_is_not_a_verified_seal(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path / "data" / "capsules", "c1")
    (capsule / "seal-bundle.json").write_text("{}")
    assert capsule_seal_verifies(capsule) is False
    assert _scan(tmp_path / "data") is False


def test_a_genuinely_sealed_capsule_counts(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path / "data" / "capsules", "c1")
    _seal(capsule, tmp_path)
    assert capsule_seal_verifies(capsule) is True
    assert _scan(tmp_path / "data") is True


def test_a_sealed_capsule_edited_after_sealing_does_not_count(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path / "data" / "capsules", "c1")
    _seal(capsule, tmp_path)
    with (capsule / "model-calls.jsonl").open("a") as fh:
        fh.write('{"model":"edited"}\n')
    assert capsule_seal_verifies(capsule) is False
    assert _scan(tmp_path / "data") is False


def _seal_binding_status(capsule: Path) -> list[str]:
    profile = load_profile("iso42001")
    seal_controls = {
        c.id for c in profile.controls
        if any(r.evidence_type == "seal_signature" for r in c.evidence_requirements)
    }
    assert seal_controls, "the profile no longer asks for a seal"
    refs = [
        ref.path
        for b in build_bindings(capsule, profile) if b.criterion_id in seal_controls
        for ref in b.evidence_refs
    ]
    return refs


def test_the_evidence_binding_cites_only_a_verifying_seal(tmp_path: Path) -> None:
    fake = _capsule(tmp_path / "a", "fake")
    (fake / "seal-bundle.json").write_text("{}")
    assert ".seal/manifest.dsse" not in _seal_binding_status(fake)
    assert "seal-bundle.json" not in _seal_binding_status(fake)

    real = _capsule(tmp_path / "b", "real")
    _seal(real, tmp_path)
    assert ".seal/manifest.dsse" in _seal_binding_status(real)


def test_audit_verify_never_prints_a_seal_claim_as_verified(tmp_path: Path) -> None:
    capsule_root = tmp_path / "data" / "capsules"
    _capsule(capsule_root, "c1")
    report = AuditEngine(tmp_path / "data", load_profile("iso42001")).scan()
    data = json.loads(report.model_dump_json())
    data["evidence_seal_verified"] = True  # a hand-edited claim
    path = tmp_path / "report.json"
    path.write_text(json.dumps(data))
    res = runner.invoke(audit_app, ["verify", str(path)])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "Seals : verified" not in flat
    assert "claimed by the report" in flat
    assert "not signed" in flat


@pytest.mark.parametrize("missing", [True, False])
def test_audit_map_exits_1_when_a_required_control_has_no_evidence(
    tmp_path: Path, missing: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.compliance.audit import engine as engine_mod

    data = tmp_path / "data"
    (data / "capsules").mkdir(parents=True)
    if not missing:
        monkeypatch.setattr(engine_mod, "_has_evidence", lambda _cd, _t: True)
        _capsule(data / "capsules", "c1")
    res = runner.invoke(audit_app, ["map", "--data-dir", str(data), "--profile", "iso42001"])
    assert res.exit_code == (1 if missing else 0), res.output
