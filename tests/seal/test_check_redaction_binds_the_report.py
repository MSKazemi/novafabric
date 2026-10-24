"""``nova verify --check-redaction`` checks that the seal covers the report it names.

Review cli-b-02. ``nova verify --check-redaction report.seal.json path/to/my-capsule/``
(the documented form) checked only that the envelope verified under the key it
carried — any self-made key passes that — and returned before looking at the
positional at all: a tampered capsule, or a path that did not exist, exited 0 with
"Redaction proof seal is VALID". The positional is now the report JSON the seal must
cover (``X.json`` for ``X.seal.json``), the signed payload must be exactly that
report, and the signer's identity is reported (``SELF-ASSERTED`` without
``--ca-bundle``; chain-validated, or failed, with it).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

REPORT = {"subject_id_hmac": "sha256:abc", "records": [], "legal_hold_mode": True}


def _self_signed(tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / f"{name}.key", tmp_path / f"{name}.crt"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    config = tmp_path / f"{name}-novaseal.yaml"
    config.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\ntsa_url: \n"
        f"merkle_db: {tmp_path / 'merkle.db'}\n"
    )
    return config, cert_path, key_path


@pytest.fixture
def sealed_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """(report, seal, signer cert) written by ``nova subject-proof``'s own sealing path."""
    from novafabric.cli.redact import _try_seal_proof_report

    config, cert, _ = _self_signed(tmp_path, "signer")
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config))
    report = tmp_path / "proof.json"
    body = json.dumps(REPORT, indent=2).encode()
    report.write_bytes(body)
    _try_seal_proof_report(report, body)
    seal = tmp_path / "proof.seal.json"
    assert seal.is_file()
    return report, seal, cert


def _verify(*args: str):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["verify", *args])


def test_a_seal_over_its_report_verifies_and_says_self_asserted(
    sealed_report: tuple[Path, Path, Path],
) -> None:
    report, seal, _ = sealed_report
    res = _verify("--check-redaction", str(seal), str(report))
    assert res.exit_code == 0, res.output
    assert "covers the report" in res.output
    assert "SELF-ASSERTED" in res.output


def test_an_edited_report_fails(sealed_report: tuple[Path, Path, Path]) -> None:
    report, seal, _ = sealed_report
    edited = dict(REPORT, records=[{"run_id": "r1"}])
    report.write_text(json.dumps(edited))
    res = _verify("--check-redaction", str(seal), str(report))
    assert res.exit_code == 1, res.output
    assert "does not cover" in res.output


def test_a_capsule_directory_or_missing_path_is_a_usage_error(
    sealed_report: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    _, seal, _ = sealed_report
    capsule = tmp_path / "my-capsule"
    capsule.mkdir()
    for positional in (capsule, tmp_path / "does-not-exist"):
        res = _verify("--check-redaction", str(seal), str(positional))
        assert res.exit_code == 2, (positional, res.output)


def test_a_seal_by_a_key_outside_the_ca_bundle_fails(
    sealed_report: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    report, seal, signer_cert = sealed_report
    _, other_ca, _ = _self_signed(tmp_path, "someone-else")
    res = _verify("--check-redaction", str(seal), "--ca-bundle", str(other_ca), str(report))
    assert res.exit_code == 1, res.output
    # Its own certificate as the anchor: the chain check passes.
    res = _verify("--check-redaction", str(seal), "--ca-bundle", str(signer_cert), str(report))
    assert res.exit_code == 0, res.output
    assert "SELF-ASSERTED" not in res.output


def test_a_seal_of_another_intent_fails(
    sealed_report: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A proof report is sealed with intent ``verified``; any other intent is not one."""
    from novafabric.trust.novaseal.config import load_signing_profile
    from novafabric.trust.novaseal.envelope import SigningIntent, bind_intent, create_envelope

    report, seal, _ = sealed_report
    profile = load_signing_profile()
    assert profile is not None
    payload = json.dumps(
        bind_intent(json.loads(report.read_bytes()), SigningIntent.AUTHORED),
        sort_keys=True, separators=(",", ":"),
    ).encode()
    seal.write_bytes(create_envelope(
        payload, profile.key_path, profile.cert_path, intent=SigningIntent.AUTHORED
    ))
    res = _verify("--check-redaction", str(seal), str(report))
    assert res.exit_code == 1, res.output
