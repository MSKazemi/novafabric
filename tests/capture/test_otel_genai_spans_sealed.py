"""``--emit-otel-genai`` writes its spans inside the sealed set (NF-032, ADR-0251).

``otel-genai-spans.json`` is written by the orchestrator before finalization, so the
ADR-0009 residual pass scans it, ``evidence_digests`` binds it, and ``nova verify``
on a sealed capsule fails when it is edited -- it is never "not covered by the seal".
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture.orchestrator import (
    OTEL_GENAI_SPANS_REF,
    CaptureOrchestrator,
)
from novafabric.cli.main import app

_WORKLOAD = [sys.executable, "-c", "print('hi')"]


@pytest.fixture()
def seal_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A local NovaSeal signing profile (self-signed P-256, no TSA)."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OTel-Spans-Seal-Test")])
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
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {tmp_path / 'merkle.db'}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config_path))
    return config_path


def _manifest(capsule: Path) -> dict:
    return yaml.safe_load((capsule / "capsule.yaml").read_text())


def test_spans_are_bound_scanned_and_verified(tmp_path: Path, seal_config: Path) -> None:
    result = CaptureOrchestrator(base_dir=tmp_path / "runs", emit_otel_genai=True).run(
        command=_WORKLOAD
    )
    capsule = result.capsule_dir
    spans_path = capsule / OTEL_GENAI_SPANS_REF
    assert result.sealed is True
    assert spans_path.is_file()
    spans = json.loads(spans_path.read_text())
    assert result.otel_genai_span_count == len(spans) >= 1
    assert spans[0]["attributes"]["gen_ai.operation.name"] == "invoke_agent"

    # Bound by the signed manifest's digest map, and scanned by the residual pass.
    assert OTEL_GENAI_SPANS_REF in _manifest(capsule)["evidence_digests"]
    proof = json.loads((capsule / "redaction-proof.json").read_text())
    assert OTEL_GENAI_SPANS_REF in {t["ref"] for t in proof["targets"]}

    runner = CliRunner()
    verified = runner.invoke(app, ["verify", str(capsule), "--seal-config", str(seal_config)])
    assert verified.exit_code == 0, verified.output
    assert "not covered by the seal" not in verified.output, verified.output

    # Editing the spans after the seal is detected.
    spans[0]["attributes"]["gen_ai.agent.name"] = "someone-else"
    spans_path.write_text(json.dumps(spans, indent=2))
    tampered = runner.invoke(app, ["verify", str(capsule), "--seal-config", str(seal_config)])
    assert tampered.exit_code == 1, tampered.output


def test_spans_are_not_written_by_default(tmp_path: Path) -> None:
    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(command=_WORKLOAD)
    assert not (result.capsule_dir / OTEL_GENAI_SPANS_REF).exists()
    assert result.otel_genai_span_count is None
    assert OTEL_GENAI_SPANS_REF not in _manifest(result.capsule_dir)["evidence_digests"]


def test_span_emission_failure_never_fails_the_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import novafabric.otel.genai_emitter as emitter

    def _boom(*_a: object, **_k: object) -> list[dict]:
        raise RuntimeError("emitter exploded")

    monkeypatch.setattr(emitter, "emit_spans", _boom)
    result = CaptureOrchestrator(base_dir=tmp_path / "runs", emit_otel_genai=True).run(
        command=_WORKLOAD
    )
    assert result.exit_code == 0
    assert result.otel_genai_span_count is None
    assert not (result.capsule_dir / OTEL_GENAI_SPANS_REF).exists()
    assert "OTel GenAI span emission failed" in capsys.readouterr().err


def test_cli_flag_writes_spans_inside_the_digest_map(tmp_path: Path) -> None:
    out_base = tmp_path / "runs"
    result = CliRunner().invoke(
        app,
        ["capture", "--no-daemon", "--emit-otel-genai", "-o", str(out_base), *_WORKLOAD],
    )
    assert result.exit_code == 0, result.output
    assert "OTel GenAI spans" in result.output
    [capsule] = [p.parent for p in out_base.glob(f"*/{OTEL_GENAI_SPANS_REF}")]
    digests = _manifest(capsule)["evidence_digests"]
    assert OTEL_GENAI_SPANS_REF in digests
    assert digests[OTEL_GENAI_SPANS_REF]["size_bytes"] == (
        (capsule / OTEL_GENAI_SPANS_REF).stat().st_size
    )
