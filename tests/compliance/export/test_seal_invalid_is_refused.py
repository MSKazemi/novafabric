"""A capsule whose ``.seal`` exists but fails verification is refused, not exported as unsealed.

``capsule_seal_verifies`` answers False both for a capsule that was never sealed and for
one that was sealed and then edited. The Art. 12 report, the GDPR RoPA entry and the FDA
PCCP document asked only that Boolean, so a tampered capsule exported as if it had never
been sealed (Art. 12 still rendering the edited bytes as evidence; RoPA and PCCP quietly
dropping the signing measure). Each now asks ``capsule_seal_status``: ``absent`` stays
visibly unsigned, ``valid`` keeps the signed output, ``invalid`` is refused.

A valid seal here means the bytes it names are unchanged since sealing; it says nothing
about who signed them.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests
from novafabric.cli.main import app
from novafabric.compliance.export import euaiact_art12, examiner, gdpr_ropa
from novafabric.compliance.export._seal_guard import CapsuleSealInvalidError
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.capsule_verify import capsule_seal_status

runner = CliRunner()

# every way a ``.seal`` can be present and not verify
DAMAGES = [
    "edit-stream",
    "edit-manifest",
    "corrupt-dsse",
    "empty-dsse",
    "seal-is-a-file",
    "seal-is-a-symlink",
    "seal-is-a-dangling-symlink",
    "seal-is-a-fifo",
]


def _sealed_capsule(root: Path, name: str = "sealed", *, bind_evidence: bool = True) -> Path:
    """A capsule with a genuine NovaSeal ``.seal/`` (local key, no TSA).

    ``bind_evidence=False`` seals a manifest without ``evidence_digests``: the shape of a
    seal made before ADR-0251, which signs the manifest only."""
    work = root / f"_seal-{name}"
    work.mkdir(parents=True)
    cap = root / name
    cap.mkdir()
    (cap / "model-calls.jsonl").write_text('{"model":"m"}\n', encoding="utf-8")
    (cap / "tool-calls.jsonl").write_text('{"tool":"t"}\n', encoding="utf-8")
    (cap / "trace.jsonl").write_text('{"span":"s"}\n', encoding="utf-8")
    manifest: dict[str, Any] = {
        "run_id": f"r-{name}",
        "status": "success",
        "created_at": "2024-01-01T00:00:00.000000Z",
        "finished_at": "2024-01-01T00:00:10.000000Z",
    }
    if bind_evidence:
        manifest["evidence_digests"] = evidence_digests(cap)
    (cap / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "seal-guard-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
    )
    key_path, cert_path = work / "seal.key", work / "seal.crt"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    bundle = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="", db_path=str(work / "merkle.db"),
    ).seal(manifest)
    seal_dir = cap / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return cap


def _unsealed_capsule(root: Path, name: str = "unsealed") -> Path:
    cap = root / name
    cap.mkdir()
    (cap / "model-calls.jsonl").write_text('{"model":"m"}\n', encoding="utf-8")
    (cap / "capsule.yaml").write_text(
        yaml.safe_dump({"run_id": f"r-{name}", "status": "success"}), encoding="utf-8"
    )
    return cap


def _damage(cap: Path, how: str, root: Path) -> None:
    """Make *cap*'s seal present but not verifying (or a capsule edited after sealing)."""
    if how == "edit-stream":
        with (cap / "model-calls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"model":"edited"}\n')
    elif how == "edit-manifest":
        with (cap / "capsule.yaml").open("a", encoding="utf-8") as fh:
            fh.write("status_note: edited after sealing\n")
    elif how == "corrupt-dsse":
        (cap / ".seal" / "manifest.dsse").write_bytes(b"not a dsse envelope")
    elif how == "empty-dsse":
        (cap / ".seal" / "manifest.dsse").write_bytes(b"")
    elif how == "seal-is-a-file":
        shutil.rmtree(cap / ".seal")
        (cap / ".seal").write_text("{}", encoding="utf-8")
    elif how == "seal-is-a-symlink":
        # a link to a genuine seal is still not this capsule's seal
        other = _sealed_capsule(root, f"donor-{cap.name}")
        shutil.rmtree(cap / ".seal")
        (cap / ".seal").symlink_to(other / ".seal")
    elif how == "seal-is-a-dangling-symlink":
        shutil.rmtree(cap / ".seal")
        (cap / ".seal").symlink_to(root / "nowhere")
    elif how == "seal-is-a-fifo":
        shutil.rmtree(cap / ".seal")
        os.mkfifo(cap / ".seal")
    else:  # pragma: no cover
        raise AssertionError(how)
    assert capsule_seal_status(cap) == "invalid", how


def _flat(output: str) -> str:
    """Rich wraps the error panel: drop borders and join lines before matching."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", output).split())


def _assert_cli_refused(result: Any, out: Path, *capsules: Path) -> None:
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit), result.exception
    flat = _flat(result.output)
    assert "seal verification" in flat
    assert "nova verify" in flat
    # rich wraps a long temp path mid-word, so compare with all whitespace removed
    squashed = "".join(flat.split())
    assert any(c.name in squashed for c in capsules)
    assert not out.exists()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)


# ---------------------------------------------------------------------------
# EU AI Act Art. 12 report
# ---------------------------------------------------------------------------


def _tamper_entry(cap: Path) -> Any:
    report = euaiact_art12.build_art12_report(cap, deployment_id="dep-1")
    return next(e for e in report.completeness_summary if e.field_name == "tamper_evidence")


def test_art12_valid_seal_keeps_the_signed_output(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    entry = _tamper_entry(cap)
    assert entry.status == "complete"
    assert "seal verifies against its files" in entry.reason


def test_art12_absent_seal_stays_visibly_unsigned(tmp_path: Path) -> None:
    entry = _tamper_entry(_unsealed_capsule(tmp_path))
    assert entry.status == "missing"
    assert "no NovaSeal seal that verifies" in entry.reason


@pytest.mark.parametrize("how", DAMAGES)
def test_art12_invalid_seal_is_refused(tmp_path: Path, how: str) -> None:
    cap = _sealed_capsule(tmp_path)
    _damage(cap, how, tmp_path)
    with pytest.raises(CapsuleSealInvalidError) as err:
        euaiact_art12.build_art12_report(cap, deployment_id="dep-1")
    assert f"nova verify {cap}" in str(err.value)


def test_art12_manifest_only_seal_with_an_edited_manifest_is_refused(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path, bind_evidence=False)
    _damage(cap, "edit-manifest", tmp_path)
    with pytest.raises(CapsuleSealInvalidError):
        euaiact_art12.build_art12_report(cap, deployment_id="dep-1")


# ---------------------------------------------------------------------------
# GDPR RoPA entry
# ---------------------------------------------------------------------------


def _ropa_measures(cap: Path) -> list[str]:
    return list(gdpr_ropa.GDPRRoPAExporter({}).build_ropa_entry(cap).security_measures)


def test_ropa_valid_seal_lists_signing(tmp_path: Path) -> None:
    assert "dsse_envelope_signing" in _ropa_measures(_sealed_capsule(tmp_path))


def test_ropa_absent_seal_stays_unsigned(tmp_path: Path) -> None:
    assert "dsse_envelope_signing" not in _ropa_measures(_unsealed_capsule(tmp_path))


@pytest.mark.parametrize("how", DAMAGES)
def test_ropa_invalid_seal_is_refused(tmp_path: Path, how: str) -> None:
    cap = _sealed_capsule(tmp_path)
    _damage(cap, how, tmp_path)
    with pytest.raises(CapsuleSealInvalidError):
        gdpr_ropa.GDPRRoPAExporter({}).build_ropa_entry(cap)


def test_ropa_register_refuses_when_any_capsule_is_invalid(tmp_path: Path) -> None:
    good = _sealed_capsule(tmp_path, "good")
    bad = _sealed_capsule(tmp_path, "bad")
    _damage(bad, "edit-stream", tmp_path)
    with pytest.raises(CapsuleSealInvalidError):
        gdpr_ropa.GDPRRoPAExporter({}).build_ropa_document([good, bad])


def test_ropa_cli_valid_seal_writes_the_entry_with_signing(tmp_path: Path) -> None:
    cap, out = _sealed_capsule(tmp_path), tmp_path / "ropa.json"
    result = runner.invoke(app, ["export-ropa", str(cap), "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert "dsse_envelope_signing" in out.read_text(encoding="utf-8")


def test_ropa_cli_absent_seal_still_exports_unsigned(tmp_path: Path) -> None:
    cap, out = _unsealed_capsule(tmp_path), tmp_path / "ropa.json"
    result = runner.invoke(app, ["export-ropa", str(cap), "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert "dsse_envelope_signing" not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("how", DAMAGES)
def test_ropa_cli_invalid_seal_exits_2_with_no_output(tmp_path: Path, how: str) -> None:
    cap, out = _sealed_capsule(tmp_path), tmp_path / "ropa.json"
    _damage(cap, how, tmp_path)
    result = runner.invoke(app, ["export-ropa", str(cap), "--output", str(out)])
    _assert_cli_refused(result, out, cap)


# ---------------------------------------------------------------------------
# FDA PCCP (two capsules: an invalid seal in EITHER one is not "signed")
# ---------------------------------------------------------------------------


def _pccp(root: Path, baseline: str, proposed: str) -> dict[str, Any]:
    out = examiner.PCCPExporter(baseline, proposed, root).export(root / "out" / "pccp.json")
    section = json.loads(out.read_text(encoding="utf-8"))["section_3_impact_assessment"]
    assert isinstance(section, dict)
    return section


def test_pccp_both_valid_lists_signing(tmp_path: Path) -> None:
    _sealed_capsule(tmp_path, "base")
    _sealed_capsule(tmp_path, "prop")
    assert _pccp(tmp_path, "base", "prop")["risk_controls"] == ["NovaSeal signing"]


def test_pccp_absent_seals_stay_unsigned(tmp_path: Path) -> None:
    _unsealed_capsule(tmp_path, "base")
    _unsealed_capsule(tmp_path, "prop")
    assert _pccp(tmp_path, "base", "prop")["risk_controls"] == []


def test_pccp_one_valid_one_absent_is_not_signed(tmp_path: Path) -> None:
    _sealed_capsule(tmp_path, "base")
    _unsealed_capsule(tmp_path, "prop")
    assert _pccp(tmp_path, "base", "prop")["risk_controls"] == []


@pytest.mark.parametrize("how", DAMAGES)
@pytest.mark.parametrize("broken", ["baseline", "proposed"])
@pytest.mark.parametrize("other", ["valid", "absent"])
def test_pccp_invalid_seal_in_either_capsule_is_refused(
    tmp_path: Path, how: str, broken: str, other: str
) -> None:
    def make(name: str, is_broken: bool) -> None:
        if is_broken:
            _damage(_sealed_capsule(tmp_path, name), how, tmp_path)
        elif other == "valid":
            _sealed_capsule(tmp_path, name)
        else:
            _unsealed_capsule(tmp_path, name)

    make("base", broken == "baseline")
    make("prop", broken == "proposed")
    out = tmp_path / "out" / "pccp.json"
    with pytest.raises(CapsuleSealInvalidError):
        examiner.PCCPExporter("base", "prop", tmp_path).export(out)
    assert not out.exists()
    assert not out.parent.exists()  # refused before anything was created


def test_pccp_same_capsule_as_baseline_and_proposed(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path, "one")
    assert _pccp(tmp_path, "one", "one")["risk_controls"] == ["NovaSeal signing"]
    _damage(cap, "edit-stream", tmp_path)
    with pytest.raises(CapsuleSealInvalidError):
        examiner.PCCPExporter("one", "one", tmp_path).export(tmp_path / "out" / "pccp.json")


def _pccp_cli(root: Path, out: Path) -> Any:
    return runner.invoke(app, [
        "export-examiner", "pccp", "--baseline", "base", "--proposed", "prop",
        "--data-dir", str(root), "--output", str(out),
    ])


def test_pccp_cli_valid_seals_write_the_document(tmp_path: Path) -> None:
    _sealed_capsule(tmp_path, "base")
    _sealed_capsule(tmp_path, "prop")
    out = tmp_path / "pccp.json"
    result = _pccp_cli(tmp_path, out)
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["section_3_impact_assessment"]["risk_controls"] == [
        "NovaSeal signing"
    ]


def test_pccp_cli_absent_seals_still_export_unsigned(tmp_path: Path) -> None:
    _unsealed_capsule(tmp_path, "base")
    _unsealed_capsule(tmp_path, "prop")
    out = tmp_path / "pccp.json"
    result = _pccp_cli(tmp_path, out)
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["section_3_impact_assessment"]["risk_controls"] == []


@pytest.mark.parametrize("how", DAMAGES)
@pytest.mark.parametrize("broken", ["base", "prop"])
def test_pccp_cli_invalid_seal_exits_2_with_no_output(
    tmp_path: Path, how: str, broken: str
) -> None:
    # the broken capsule sits next to an UNSEALED one: absence must not hide tampering
    caps = {
        "base": _sealed_capsule(tmp_path, "base") if broken == "base"
        else _unsealed_capsule(tmp_path, "base"),
        "prop": _sealed_capsule(tmp_path, "prop") if broken == "prop"
        else _unsealed_capsule(tmp_path, "prop"),
    }
    _damage(caps[broken], how, tmp_path)
    out = tmp_path / "pccp.json"
    result = _pccp_cli(tmp_path, out)
    _assert_cli_refused(result, out, caps[broken])


def test_corrupt_envelope_payload_is_refused_everywhere(tmp_path: Path) -> None:
    """A structurally valid envelope whose signed payload was altered is invalid too."""
    cap = _sealed_capsule(tmp_path)
    path = cap / ".seal" / "manifest.dsse"
    env = json.loads(path.read_text(encoding="utf-8"))
    payload = json.loads(base64.b64decode(env["payload"]))
    payload["tampered"] = True
    env["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    path.write_text(json.dumps(env), encoding="utf-8")
    assert capsule_seal_status(cap) == "invalid"
    with pytest.raises(CapsuleSealInvalidError):
        euaiact_art12.build_art12_report(cap, deployment_id="dep-1")
    with pytest.raises(CapsuleSealInvalidError):
        gdpr_ropa.GDPRRoPAExporter({}).build_ropa_entry(cap)


# ---------------------------------------------------------------------------
# HTTP surface: the same exporters back the dashboard routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["edit-stream", "seal-is-a-file"])
@pytest.mark.parametrize("route", ["ropa", "examiner"])
def test_http_exports_do_not_succeed_on_an_invalid_seal(
    tmp_path: Path, how: str, route: str
) -> None:
    """The routes must not return a document for a capsule whose seal fails.

    (The status code is the route's generic export failure; the message names the cause.)"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    runs = tmp_path / "runs"
    runs.mkdir()
    cap = _sealed_capsule(runs, "01SEALED000000000000000001")
    _damage(cap, how, tmp_path)
    token = "seal-guard-token-1234"
    client = TestClient(
        create_app(token=token, capsule_dir=runs, db_path=tmp_path / "r.db", static_dir=None),
        raise_server_exceptions=False,
    )
    body: dict[str, Any] = {"run_id": cap.name}
    path = "/api/compliance/export/ropa"
    if route == "examiner":
        path = "/api/compliance/examiner/pccp"
    r = client.post(path, json=body, params={"token": token},
                    headers={"host": "127.0.0.1:4321"})
    assert r.status_code >= 400, r.text
    assert "seal verification" in r.text
