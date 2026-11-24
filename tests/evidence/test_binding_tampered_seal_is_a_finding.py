"""Criterion bindings tell a tampered seal apart from an absent one.

``build_bindings`` cited the seal only when it verified, and scored a sealed-then-edited
capsule the same as one never sealed: an empty reference list, nothing said. A binding
for a control that asks for seal evidence now carries an explicit ``findings`` entry when
the capsule's ``.seal`` exists but fails verification. ``absent`` stays "no seal
evidence" (no finding), a valid seal is unchanged, and a binding with no finding
serialises exactly as before (the field is additive and omitted when empty).
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.capture.finalize import evidence_digests
from novafabric.compliance.audit.loader import load_profile
from novafabric.evidence.binding import CriterionBinding, build_bindings
from novafabric.trust.novaseal import KeyConfig, NovaSeal

KINDS = [
    "valid", "absent", "tampered_member", "tampered_manifest",
    "corrupt_envelope", "seal_symlink", "seal_regular_file",
]
INVALID_KINDS = [k for k in KINDS if k not in ("valid", "absent")]


def _seal_key(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "binding-test")])
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


def _make(tmp_path: Path, kind: str) -> Path:
    capsule = tmp_path / "cap"
    capsule.mkdir()
    (capsule / "model-calls.jsonl").write_text('{"model":"m"}\n')
    (capsule / "capsule.yaml").write_text(yaml.safe_dump({"run_id": "cap", "status": "success"}))
    if kind == "absent":
        return capsule
    work = tmp_path / "work"
    work.mkdir()
    key_path, cert_path = _seal_key(work)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    bundle = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="", db_path=str(work / "merkle.db"),
    ).seal(manifest)
    seal = capsule / ".seal"
    seal.mkdir()
    (seal / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal / "log-entry.json").write_text(json.dumps(bundle.log_entry))
    if kind == "tampered_member":
        with (capsule / "model-calls.jsonl").open("a") as fh:
            fh.write('{"model":"edited"}\n')
    elif kind == "tampered_manifest":
        with (capsule / "capsule.yaml").open("a") as fh:
            fh.write("status_note: edited after sealing\n")
    elif kind == "corrupt_envelope":
        (seal / "manifest.dsse").write_bytes(b"{ not a dsse envelope")
    elif kind == "seal_symlink":
        real = work / "real-seal"
        seal.rename(real)
        os.symlink(real, seal)
    elif kind == "seal_regular_file":
        for child in seal.iterdir():
            child.unlink()
        seal.rmdir()
        seal.write_text("not a directory")
    return capsule


def _split(capsule: Path) -> tuple[list[CriterionBinding], list[CriterionBinding]]:
    """(bindings of controls asking for a seal, the rest) under the iso42001 profile."""
    profile = load_profile("iso42001")
    seal_controls = {
        c.id for c in profile.controls
        if any(r.evidence_type == "seal_signature" for r in c.evidence_requirements)
    }
    assert seal_controls, "the profile no longer asks for a seal"
    bindings = build_bindings(capsule, profile)
    return (
        [b for b in bindings if b.criterion_id in seal_controls],
        [b for b in bindings if b.criterion_id not in seal_controls],
    )


@pytest.mark.parametrize("kind", INVALID_KINDS)
def test_an_invalid_seal_is_a_finding_on_the_seal_controls(tmp_path: Path, kind: str) -> None:
    seal_bindings, other = _split(_make(tmp_path, kind))
    for b in seal_bindings:
        assert ".seal/manifest.dsse" not in [r.path for r in b.evidence_refs]
        assert [f.code for f in b.findings] == ["seal_invalid"], b
        assert "failed seal verification" in b.findings[0].message
    assert all(b.findings == [] for b in other)


@pytest.mark.parametrize("kind", ["valid", "absent"])
def test_valid_and_absent_seals_carry_no_finding(tmp_path: Path, kind: str) -> None:
    seal_bindings, other = _split(_make(tmp_path, kind))
    assert all(b.findings == [] for b in [*seal_bindings, *other])
    cited = {r.path for b in seal_bindings for r in b.evidence_refs}
    assert (".seal/manifest.dsse" in cited) is (kind == "valid")


@pytest.mark.parametrize("kind", INVALID_KINDS)
def test_absent_and_invalid_bindings_differ(tmp_path: Path, kind: str) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    unsigned, _ = _split(_make(tmp_path / "a", "absent"))
    tampered, _ = _split(_make(tmp_path / "b", kind))
    assert all(b.findings == [] for b in unsigned)
    assert all(len(b.findings) == 1 for b in tampered)


def test_a_binding_without_findings_serialises_exactly_as_before() -> None:
    binding = CriterionBinding(criterion_id="X", binding_status="unsatisfied")
    assert "findings" not in binding.model_dump()
    assert "findings" not in json.loads(binding.model_dump_json())


def test_a_binding_with_a_finding_serialises_it(tmp_path: Path) -> None:
    seal_bindings, _ = _split(_make(tmp_path, "tampered_member"))
    dumped = seal_bindings[0].model_dump()
    assert dumped["findings"][0]["code"] == "seal_invalid"


@pytest.mark.parametrize(("kind", "finding"), [("tampered_member", True), ("absent", False)])
def test_evidence_bind_cli_says_so_and_writes_the_finding(
    tmp_path: Path, kind: str, finding: bool
) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.evidence import evidence_app

    capsule = _make(tmp_path, kind)
    out = tmp_path / "bindings.json"
    res = CliRunner().invoke(
        evidence_app,
        ["bind", str(capsule), "--profile", "iso42001", "--output", str(out)],
    )
    assert res.exit_code == 0, res.output
    assert ("failed seal verification" in " ".join(res.output.split())) is finding
    codes = {f["code"] for b in json.loads(out.read_text())["bindings"]
             for f in b.get("findings", [])}
    assert codes == ({"seal_invalid"} if finding else set())
