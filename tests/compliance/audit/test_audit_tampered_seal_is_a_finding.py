"""A sealed-then-edited capsule is a FINDING in the audit, not just missing seal evidence.

``capsule_seal_verifies`` answers False both for a capsule that was never sealed and for
one whose ``.seal`` exists but fails verification, so the audit scored a tampered capsule
exactly like an unsigned one and never said so. The audit now tells the two apart:

* ``absent``  — no ``.seal``: "no seal evidence", unchanged, no finding;
* ``valid``   — the seal verifies: unchanged, no finding;
* ``invalid`` — a ``.seal`` that fails (edited member, edited manifest, damaged envelope,
  a linked or non-directory ``.seal``): an explicit ``seal_invalid`` finding.

A valid seal means the bytes it names are unchanged since sealing; it does not say who
signed them.
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
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests
from novafabric.cli.audit import app as audit_app
from novafabric.compliance.audit import engine as engine_mod
from novafabric.compliance.audit.engine import AuditEngine
from novafabric.compliance.audit.loader import load_profile
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.capsule_verify import capsule_seal_status

runner = CliRunner()

KINDS = [
    "valid",
    "absent",
    "tampered_member",
    "tampered_manifest",
    "corrupt_envelope",
    "seal_symlink",
    "seal_regular_file",
]
INVALID_KINDS = [k for k in KINDS if k not in ("valid", "absent")]


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


def _seal(capsule: Path, work: Path) -> None:
    work.mkdir(parents=True, exist_ok=True)
    key_path, cert_path = _seal_key(work)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    bundle = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="", db_path=str(work / "merkle.db"),
    ).seal(manifest)
    seal_dir = capsule / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry))


def _make(root: Path, name: str, kind: str, work: Path) -> Path:
    """A capsule in the state *kind* describes (``root`` is the capsules directory)."""
    capsule = _capsule(root, name)
    if kind == "absent":
        return capsule
    _seal(capsule, work)
    seal = capsule / ".seal"
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
    else:
        assert kind == "valid", kind
    return capsule


def _scan(data_dir: Path, **kw: object):  # type: ignore[no-untyped-def]
    return AuditEngine(data_dir, load_profile("iso42001"), **kw).scan()  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", KINDS)
def test_the_status_each_kind_is_built_to_have(tmp_path: Path, kind: str) -> None:
    capsule = _make(tmp_path / "c", "c1", kind, tmp_path / "w")
    expected = {"valid": "valid", "absent": "absent"}.get(kind, "invalid")
    assert capsule_seal_status(capsule) == expected


@pytest.mark.parametrize("kind", INVALID_KINDS)
def test_an_invalid_seal_is_an_explicit_finding(tmp_path: Path, kind: str) -> None:
    data = tmp_path / "data"
    _make(data / "capsules", "c1", kind, tmp_path / "w")
    report = _scan(data)
    assert [f.code for f in report.findings] == ["seal_invalid"]
    finding = report.findings[0]
    assert finding.capsule_id == "c1"
    assert "failed seal verification" in finding.message
    assert report.evidence_seal_verified is False  # still not seal evidence


def test_a_valid_seal_has_no_finding_and_counts(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _make(data / "capsules", "c1", "valid", tmp_path / "w")
    report = _scan(data)
    assert report.findings == []
    assert report.evidence_seal_verified is True


def test_an_absent_seal_has_no_finding_and_is_just_no_seal_evidence(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _make(data / "capsules", "c1", "absent", tmp_path / "w")
    report = _scan(data)
    assert report.findings == []
    assert report.evidence_seal_verified is False


@pytest.mark.parametrize("kind", INVALID_KINDS)
def test_absent_and_invalid_produce_different_outcomes(tmp_path: Path, kind: str) -> None:
    unsigned = _scan(_data_with(tmp_path / "a", "absent"))
    tampered = _scan(_data_with(tmp_path / "b", kind))
    assert unsigned.evidence_seal_verified is tampered.evidence_seal_verified is False
    assert unsigned.findings != tampered.findings
    assert unsigned.findings == [] and len(tampered.findings) == 1


def _data_with(base: Path, kind: str) -> Path:
    data = base / "data"
    _make(data / "capsules", "c1", kind, base / "w")
    return data


def test_one_tampered_capsule_among_valid_ones_is_still_reported(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _make(data / "capsules", "good", "valid", tmp_path / "w1")
    _make(data / "capsules", "bad", "tampered_member", tmp_path / "w2")
    _make(data / "capsules", "unsigned", "absent", tmp_path / "w3")
    report = _scan(data)
    assert report.evidence_seal_verified is True  # one capsule's seal verified: unchanged
    assert [(f.code, f.capsule_id) for f in report.findings] == [("seal_invalid", "bad")]


def test_a_single_capsule_audit_reports_its_own_tampering(tmp_path: Path) -> None:
    data = tmp_path / "data"
    bad = _make(data / "capsules", "bad", "tampered_manifest", tmp_path / "w")
    report = _scan(data, capsule_paths=[bad])
    assert [f.capsule_id for f in report.findings] == ["bad"]


def test_the_finding_is_serialised_additively(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _make(data / "capsules", "c1", "tampered_member", tmp_path / "w")
    payload = json.loads(_scan(data).model_dump_json(by_alias=True, exclude_none=True))
    assert payload["findings"][0]["code"] == "seal_invalid"
    assert payload["findings"][0]["capsule_id"] == "c1"


# -- the CLI: no silent pass -------------------------------------------------------------


def _all_controls_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every control satisfied, so the only thing left to fail an audit is the seal."""
    monkeypatch.setattr(engine_mod, "_has_evidence", lambda _cd, _t: True)


@pytest.mark.parametrize("kind", INVALID_KINDS)
def test_audit_map_exits_1_and_names_the_tampered_capsule(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data_with(tmp_path, kind)
    res = runner.invoke(audit_app, ["map", "--data-dir", str(data), "--profile", "iso42001"])
    flat = " ".join(res.output.split())
    assert res.exit_code == 1, res.output
    assert "failed seal verification" in flat
    assert "c1" in flat
    assert "Seals : verified" not in flat


@pytest.mark.parametrize("kind", ["valid", "absent"])
def test_audit_map_exit_is_unchanged_without_a_tampered_capsule(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data_with(tmp_path, kind)
    res = runner.invoke(audit_app, ["map", "--data-dir", str(data), "--profile", "iso42001"])
    assert res.exit_code == 0, res.output
    assert "failed seal verification" not in " ".join(res.output.split())


def test_audit_map_json_carries_the_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data_with(tmp_path, "tampered_member")
    res = runner.invoke(
        audit_app, ["map", "--data-dir", str(data), "--profile", "iso42001", "--json"]
    )
    assert res.exit_code == 1, res.output
    assert json.loads(res.output)["findings"][0]["code"] == "seal_invalid"


def test_audit_report_text_lists_the_finding(tmp_path: Path) -> None:
    data = _data_with(tmp_path, "tampered_member")
    res = runner.invoke(
        audit_app,
        ["report", "--data-dir", str(data), "--profile", "iso42001", "--format", "text"],
    )
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "FINDING" in flat and "seal_invalid" in flat and "c1" in flat


@pytest.mark.parametrize(("kind", "exit_code"), [
    ("tampered_member", 1), ("seal_symlink", 1), ("absent", 0), ("valid", 0),
])
def test_audit_coverage_gate_does_not_pass_a_tampered_capsule(
    tmp_path: Path, kind: str, exit_code: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_controls_covered(monkeypatch)
    data = _data_with(tmp_path, kind)
    res = runner.invoke(
        audit_app, ["coverage", "--data-dir", str(data), "--profile", "iso42001"]
    )
    flat = " ".join(res.output.split())
    assert res.exit_code == exit_code, res.output
    assert ("failed seal verification" in flat) is (exit_code == 1)
    assert ("PASS" in flat) is (exit_code == 0)
