"""The trust radar and `nova verify` share one verification path (audit H1, 2026-10-22).

`trust/capsule_flags.py` ran the NovaSeal signature, timestamp and log checks
only. It never ran the ADR-0251 capsule-binding checks (`capsule.yaml` against
the signed payload, `evidence_digests` against the files) that `nova verify`
runs. A capsule edited after sealing, or one carrying another capsule's
`.seal/`, rendered **ATTESTED** on the radar and in the dashboard Seal tab
while `nova verify` exited 1.

Every test perturbs the capsule after sealing and then asks both surfaces. The
property: the radar is never `attested` for a capsule `nova verify` rejects.
"""

from __future__ import annotations

import datetime
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests
from novafabric.cli.main import app
from novafabric.trust.capsule_flags import flags_from_capsule
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.radar import AxisState, RadarVerdict, build_trust_radar

runner = CliRunner()


def _identity(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A P-256 key, a self-signed certificate and a ``novaseal.yaml`` naming both."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "H1-radar-test")])
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
    config = tmp_path / "novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {tmp_path / 'merkle.db'}\n"
    )
    return key_path, cert_path, config


def _sealed_capsule(root: Path, name: str, key_path: Path, cert_path: Path) -> Path:
    capsule = root / name
    (capsule / "outputs").mkdir(parents=True)
    (capsule / "model-calls.jsonl").write_text('{"model":"m","tokens_in":7}\n')
    (capsule / "trace.jsonl").write_text('{"span":"root"}\n')
    (capsule / "outputs" / "stdout.txt").write_text(f"hello from {name}\n")
    manifest: dict[str, object] = {"run_id": name, "status": "success"}
    manifest["evidence_digests"] = evidence_digests(capsule)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(root.parent / "merkle.db"),
    )
    bundle = seal.seal(manifest)
    seal_dir = capsule / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry))
    return capsule


@pytest.fixture()
def capsules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Two capsules sealed by one key into one log; returns ``(a, b, config)``."""
    key_path, cert_path, config = _identity(tmp_path)
    root = tmp_path / "capsules"
    root.mkdir()
    a = _sealed_capsule(root, "01H1RADARCAPSULEA000000000", key_path, cert_path)
    b = _sealed_capsule(root, "01H1RADARCAPSULEB000000000", key_path, cert_path)
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    return a, b, config


def _edit_evidence(a: Path, b: Path) -> Path:
    with (a / "model-calls.jsonl").open("a") as fh:
        fh.write('{"model":"m","tokens_in":999999}\n')
    return a


def _edit_manifest(a: Path, b: Path) -> Path:
    manifest = yaml.safe_load((a / "capsule.yaml").read_text())
    manifest["status"] = "success-FORGED"
    (a / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    return a


def _delete_evidence(a: Path, b: Path) -> Path:
    (a / "trace.jsonl").unlink()
    return a


def _transplant_seal(a: Path, b: Path) -> Path:
    """Capsule B carries capsule A's genuine, internally consistent ``.seal/``."""
    shutil.rmtree(b / ".seal")
    shutil.copytree(a / ".seal", b / ".seal")
    return b


def _forge_log_entry_id(a: Path, b: Path) -> Path:
    entry_file = a / ".seal" / "log-entry.json"
    entry = json.loads(entry_file.read_text())
    entry["entry"]["capsule_id"] = "0" * 64
    entry_file.write_text(json.dumps(entry))
    return a


TAMPERS: dict[str, Callable[[Path, Path], Path]] = {
    "evidence-edited": _edit_evidence,
    "manifest-edited": _edit_manifest,
    "evidence-deleted": _delete_evidence,
    "seal-transplanted": _transplant_seal,
    "log-entry-id-forged": _forge_log_entry_id,
}


def _verify_json(capsule: Path, config: Path) -> tuple[int, dict[str, object]]:
    result = runner.invoke(
        app, ["verify", str(capsule), "--seal-config", str(config), "--json"]
    )
    return result.exit_code, json.loads(result.output)


def test_untouched_capsule_is_attested_and_verifies(capsules: tuple[Path, Path, Path]) -> None:
    a, _, config = capsules
    radar = build_trust_radar(flags_from_capsule(a))
    code, body = _verify_json(a, config)
    assert code == 0, body
    assert body["valid"] is True
    assert radar.verdict is RadarVerdict.attested, radar


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
def test_a_capsule_nova_verify_rejects_is_never_attested(
    capsules: tuple[Path, Path, Path], tamper: str
) -> None:
    a, b, config = capsules
    target = TAMPERS[tamper](a, b)

    code, body = _verify_json(target, config)
    assert code == 1 and body["valid"] is False, f"{tamper}: nova verify accepted it"

    radar = build_trust_radar(flags_from_capsule(target))
    assert radar.verdict is RadarVerdict.critical, f"{tamper}: radar said {radar.verdict}"
    signature = next(axis for axis in radar.axes if axis.key == "signature")
    assert signature.state is AxisState.fail


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
def test_flags_report_the_binding_verdict(
    capsules: tuple[Path, Path, Path], tamper: str
) -> None:
    a, b, _ = capsules
    target = TAMPERS[tamper](a, b)
    flags = flags_from_capsule(target)
    assert flags["valid"] is False
    assert flags["capsule_binding_ok"] is False


def test_radar_verifies_a_sealed_capsule_without_a_local_profile(
    capsules: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nova verify` is self-contained (ADR-0297 S2), so the radar is too."""
    a, _, _ = capsules
    _edit_evidence(a, a)
    empty_home = a.parent.parent / "empty-home"
    empty_home.mkdir()
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG")
    monkeypatch.setenv("NOVAFABRIC_HOME", str(empty_home))
    radar = build_trust_radar(flags_from_capsule(a))
    assert radar.verdict is RadarVerdict.critical


def test_serve_verify_endpoint_reports_a_tampered_capsule_invalid(
    capsules: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    a, _, _ = capsules
    _edit_manifest(a, a)
    token = "test-token-1234567890abcdef"
    api = create_app(
        token=token, capsule_dir=a.parent, db_path=tmp_path / "registry.db", static_dir=None
    )
    with TestClient(api) as client:
        body = client.post(
            f"/api/runs/{a.name}/verify",
            params={"token": token},
            headers={"host": "127.0.0.1:4321"},
        ).json()
    assert body["signature_ok"] is True
    assert body["capsule_binding_ok"] is False
    assert body["valid"] is False
    assert any("status" in err for err in body["errors"]), body["errors"]


def test_a_linked_evidence_file_is_never_followed(
    capsules: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A symlink to a file holding the *signed* bytes still fails (R6: no link is
    followed), so an evidence file cannot be swapped for a pointer elsewhere."""
    a, _, config = capsules
    outside = tmp_path / "outside-trace.jsonl"
    outside.write_bytes((a / "trace.jsonl").read_bytes())
    (a / "trace.jsonl").unlink()
    (a / "trace.jsonl").symlink_to(outside)

    code, body = _verify_json(a, config)
    assert code == 1 and body["valid"] is False
    assert build_trust_radar(flags_from_capsule(a)).verdict is RadarVerdict.critical


def test_a_linked_envelope_fails(capsules: tuple[Path, Path, Path], tmp_path: Path) -> None:
    a, _, config = capsules
    outside = tmp_path / "outside.dsse"
    outside.write_bytes((a / ".seal" / "manifest.dsse").read_bytes())
    (a / ".seal" / "manifest.dsse").unlink()
    (a / ".seal" / "manifest.dsse").symlink_to(outside)

    code, body = _verify_json(a, config)
    assert code == 1 and body["valid"] is False
    assert any("manifest.dsse not read" in e for e in body["errors"]), body["errors"]
    assert build_trust_radar(flags_from_capsule(a)).verdict is RadarVerdict.critical
