from __future__ import annotations

import base64
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import jsonschema
import pytest
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.evidence.intoto import dsse_pae
from novafabric.evidence.signing import generate_keypair

runner = CliRunner()

SCHEMA_DIR = Path(__file__).parents[1] / "src/novafabric/schemas"
BUNDLE_SCHEMA = json.loads((SCHEMA_DIR / "evidence-bundle.schema.json").read_text())


def _make_capsule(tmp_path: Path) -> Path:
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, "-c", "pass"])
    return result.capsule_dir


def _make_keypair(tmp_path: Path) -> Path:
    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    priv_path, _pub_path = generate_keypair(keys_dir)
    return priv_path


def _read_manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("manifest.json"))


def test_happy_path_bundle_validates_against_schema(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    result = runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()

    manifest = _read_manifest(out)
    jsonschema.validate(
        manifest, BUNDLE_SCHEMA, format_checker=jsonschema.FormatChecker()
    )


def test_all_schemas_vendored_with_correct_hashes(tmp_path: Path) -> None:
    import hashlib

    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"
    runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )

    manifest = _read_manifest(out)
    schema_files = sorted(SCHEMA_DIR.glob("*.schema.json"))
    assert len(manifest["schemas"]) == len(schema_files)

    with zipfile.ZipFile(out) as zf:
        for entry in manifest["schemas"]:
            data = zf.read(f"schemas/{entry['name']}")
            actual = "sha256:" + hashlib.sha256(data).hexdigest()
            assert actual == entry["sha256"]


def test_three_attestations_present_with_correct_subjects(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"
    runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )

    manifest = _read_manifest(out)
    predicate_types = {a["predicate_type"] for a in manifest["attestations"]}
    assert "https://novafabric.io/runcapsule/v0" in predicate_types
    assert "https://novafabric.io/redaction-proof/v0" in predicate_types
    assert "https://novafabric.io/lineage/v0" in predicate_types

    capsule_hash = manifest["subject"]["capsule_hash"]
    run_attestation = next(
        a for a in manifest["attestations"]
        if a["predicate_type"] == "https://novafabric.io/runcapsule/v0"
    )
    assert capsule_hash in run_attestation["subject_hashes"]


def test_dsse_signature_roundtrip(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    pub_path = key_path.parent / "ed25519.pub.pem"
    out = tmp_path / "evidence.zip"
    runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )

    public_key = serialization.load_pem_public_key(pub_path.read_bytes())

    with zipfile.ZipFile(out) as zf:
        envelope_path = "attestations/run.intoto.json"
        envelope = json.loads(zf.read(envelope_path))
        payload = base64.b64decode(envelope["payload"])
        sig = base64.b64decode(envelope["signatures"][0]["sig"])
        pae = dsse_pae(envelope["payloadType"], payload)
        public_key.verify(sig, pae)  # raises on failure


def test_dsse_flag_emits_verifiable_outer_envelope(tmp_path: Path) -> None:
    # NF-029/ADR-0096: --dsse wraps the final bundle manifest in a standard DSSE envelope.
    from novafabric.envelopes.dsse import BUNDLE_PAYLOAD_TYPE

    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    pub_path = key_path.parent / "ed25519.pub.pem"
    out = tmp_path / "evidence.zip"
    result = runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out), "--dsse"],
    )
    assert result.exit_code == 0, result.output

    dsse_path = Path(str(out) + ".dsse.json")
    assert dsse_path.exists()
    envelope = json.loads(dsse_path.read_text())
    assert envelope["payloadType"] == BUNDLE_PAYLOAD_TYPE

    # payload is the final manifest bytes verbatim (wrap, don't replace)
    with zipfile.ZipFile(out) as zf:
        manifest_bytes = zf.read("manifest.json")
    assert base64.b64decode(envelope["payload"]) == manifest_bytes

    # signature verifies with the same key
    public_key = serialization.load_pem_public_key(pub_path.read_bytes())
    sig = base64.b64decode(envelope["signatures"][0]["sig"])
    pae = dsse_pae(envelope["payloadType"], manifest_bytes)
    public_key.verify(sig, pae)  # raises on failure


def test_no_dsse_flag_writes_no_envelope(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"
    runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )
    assert not Path(str(out) + ".dsse.json").exists()


def test_dsse_envelope_verifiable_via_verify_envelope_cli(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    pub_path = key_path.parent / "ed25519.pub.pem"
    out = tmp_path / "evidence.zip"
    runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out), "--dsse"],
    )
    dsse_path = Path(str(out) + ".dsse.json")
    result = runner.invoke(
        app, ["verify-envelope", str(dsse_path), "--key", str(pub_path)]
    )
    assert result.exit_code == 0, result.output
    assert "verified" in result.output


def test_unsafe_skips_blocks_export(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    proof_path = capsule_dir / "redaction-proof.json"
    proof = json.loads(proof_path.read_text())
    proof.setdefault("unsafe_skips", []).append({
        "finding_id": "01HXAY7M5JZ8R7K4P9DPBYK2WX",
        "rule_id": "synthetic",
        "rationale": "test",
        "decided_by": "cli",
        "decided_at": "2026-05-08T12:00:00Z",
    })
    proof_path.write_text(json.dumps(proof, indent=2))

    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"
    result = runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )
    assert result.exit_code == 2
    assert "unsafe_skips" in result.output

    result_allow = runner.invoke(
        app,
        [
            "export-evidence", str(capsule_dir),
            "--key", str(key_path),
            "--output", str(out),
            "--allow-unsafe-skips",
        ],
    )
    assert result_allow.exit_code == 0


def test_manifest_hash_detects_tampering(tmp_path: Path) -> None:
    import hashlib

    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"
    runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )
    manifest = _read_manifest(out)
    declared_hash = manifest["manifest_hash"]

    canonical = manifest.copy()
    canonical.pop("manifest_hash")
    recomputed = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert recomputed == declared_hash

    tampered = manifest.copy()
    tampered["bundle_id"] = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
    tampered.pop("manifest_hash")
    recomputed_after_tamper = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert recomputed_after_tamper != declared_hash


def test_missing_capsule_file_errors(tmp_path: Path) -> None:
    capsule_dir = _make_capsule(tmp_path)
    (capsule_dir / "trace.jsonl").unlink()
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"
    result = runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )
    assert result.exit_code == 1
    assert "trace.jsonl" in result.output or "missing" in result.output.lower()


def test_sigstore_flag_publishes_or_skips(tmp_path: Path) -> None:
    """--sigstore succeeds and either publishes to Rekor or prints the skip warning."""
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"
    result = runner.invoke(
        app,
        [
            "export-evidence", str(capsule_dir),
            "--key", str(key_path),
            "--output", str(out),
            "--sigstore",
        ],
    )
    assert result.exit_code == 0
    # Without NOVA_REKOR_URL set, the skip warning should appear
    assert "rekor" in result.output.lower()


@pytest.mark.parametrize("missing", ["model-calls.jsonl", "tool-calls.jsonl"])
def test_other_required_capsule_files_validated(tmp_path: Path, missing: str) -> None:
    capsule_dir = _make_capsule(tmp_path)
    (capsule_dir / missing).unlink()
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"
    result = runner.invoke(
        app,
        ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
    )
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# RFC 3161 timestamp integration tests (ADR-0030)
# ---------------------------------------------------------------------------


def _make_mock_tsr_response(pki_status: int) -> bytes:
    """Build a minimal DER TimeStampResp with the given PKIStatus."""
    from novafabric.trust._rfc3161 import _der_integer, _der_sequence
    status_integer = _der_integer(pki_status)
    pki_status_info = _der_sequence(status_integer)
    return _der_sequence(pki_status_info)


def _mock_tsa_post(tsr_der: bytes) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = tsr_der
    return mock_resp


def test_timestamp_flag_adds_tsr_to_bundle(tmp_path: Path) -> None:
    """--timestamp with a mocked TSA produces a bundle with manifest.dsse.tsr."""
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    tsr_der = _make_mock_tsr_response(0)  # PKIStatus=granted

    with patch(
        "novafabric.trust._rfc3161.httpx.post",
        return_value=_mock_tsa_post(tsr_der),
    ):
        result = runner.invoke(
            app,
            [
                "export-evidence", str(capsule_dir),
                "--key", str(key_path),
                "--output", str(out),
                "--timestamp",
                "--timestamp-url", "https://tsa.example.com/tsr",
            ],
        )

    assert result.exit_code == 0, result.output
    assert out.exists()

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "manifest.dsse.tsr" in names
        tsr_stored = zf.read("manifest.dsse.tsr")
        assert tsr_stored == tsr_der


def test_timestamp_manifest_updated_with_tsr_hash(tmp_path: Path) -> None:
    """--timestamp writes manifest_dsse_tsr_sha256 and timestamp_status=ok."""
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    tsr_der = _make_mock_tsr_response(0)

    with patch(
        "novafabric.trust._rfc3161.httpx.post",
        return_value=_mock_tsa_post(tsr_der),
    ):
        runner.invoke(
            app,
            [
                "export-evidence", str(capsule_dir),
                "--key", str(key_path),
                "--output", str(out),
                "--timestamp",
                "--timestamp-url", "https://tsa.example.com/tsr",
            ],
        )

    manifest = _read_manifest(out)
    assert manifest["timestamp_status"] == "ok"
    assert manifest["timestamp_tsa_url"] == "https://tsa.example.com/tsr"
    expected_sha256 = "sha256:" + hashlib.sha256(tsr_der).hexdigest()
    assert manifest["manifest_dsse_tsr_sha256"] == expected_sha256


def test_timestamp_manifest_hash_recomputed(tmp_path: Path) -> None:
    """After --timestamp, manifest_hash covers the new timestamp fields."""
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    tsr_der = _make_mock_tsr_response(0)

    with patch(
        "novafabric.trust._rfc3161.httpx.post",
        return_value=_mock_tsa_post(tsr_der),
    ):
        runner.invoke(
            app,
            [
                "export-evidence", str(capsule_dir),
                "--key", str(key_path),
                "--output", str(out),
                "--timestamp",
                "--timestamp-url", "https://tsa.example.com/tsr",
            ],
        )

    manifest = _read_manifest(out)
    declared_hash = manifest["manifest_hash"]
    work = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    recomputed = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(work, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert recomputed == declared_hash


def test_timestamp_readme_updated(tmp_path: Path) -> None:
    """--timestamp appends the RFC 3161 verification section to README.md."""
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    tsr_der = _make_mock_tsr_response(0)

    with patch(
        "novafabric.trust._rfc3161.httpx.post",
        return_value=_mock_tsa_post(tsr_der),
    ):
        runner.invoke(
            app,
            [
                "export-evidence", str(capsule_dir),
                "--key", str(key_path),
                "--output", str(out),
                "--timestamp",
                "--timestamp-url", "https://tsa.example.com/tsr",
            ],
        )

    with zipfile.ZipFile(out) as zf:
        readme = zf.read("README.md").decode()

    assert "## Timestamp (RFC 3161)" in readme
    assert "openssl ts -verify" in readme


def test_timestamp_tsa_fatal_failure_exits_nonzero(tmp_path: Path) -> None:
    """Without --timestamp-optional, TSA failure is fatal (exit code 1)."""
    import httpx as httpx_mod

    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    with patch(
        "novafabric.trust._rfc3161.httpx.post",
        side_effect=httpx_mod.TransportError("timeout"),
    ):
        result = runner.invoke(
            app,
            [
                "export-evidence", str(capsule_dir),
                "--key", str(key_path),
                "--output", str(out),
                "--timestamp",
                "--timestamp-url", "https://tsa.example.com/tsr",
            ],
        )

    assert result.exit_code == 1
    assert "timestamp" in result.output.lower()


def test_timestamp_optional_continues_on_tsa_failure(tmp_path: Path) -> None:
    """With --timestamp-optional, TSA failure writes failure status but succeeds."""
    import httpx as httpx_mod

    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    with patch(
        "novafabric.trust._rfc3161.httpx.post",
        side_effect=httpx_mod.TransportError("connection refused"),
    ):
        result = runner.invoke(
            app,
            [
                "export-evidence", str(capsule_dir),
                "--key", str(key_path),
                "--output", str(out),
                "--timestamp",
                "--timestamp-optional",
                "--timestamp-url", "https://tsa.example.com/tsr",
            ],
        )

    assert result.exit_code == 0, result.output
    assert out.exists()

    manifest = _read_manifest(out)
    assert manifest["timestamp_status"] == "failed"
    assert "timestamp_failure_reason" in manifest

    # TSR should NOT be present when timestamp failed
    with zipfile.ZipFile(out) as zf:
        assert "manifest.dsse.tsr" not in zf.namelist()


def test_timestamp_tsa_url_precedence_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOVAFABRIC_TSA_URL env var is used when --timestamp-url is not given."""
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    monkeypatch.setenv("NOVAFABRIC_TSA_URL", "https://env-tsa.example.com/ts")

    tsr_der = _make_mock_tsr_response(0)
    captured_urls: list[str] = []

    def capture_post(url: str, **kwargs: object) -> MagicMock:
        captured_urls.append(url)
        return _mock_tsa_post(tsr_der)

    with patch("novafabric.trust._rfc3161.httpx.post", side_effect=capture_post):
        result = runner.invoke(
            app,
            [
                "export-evidence", str(capsule_dir),
                "--key", str(key_path),
                "--output", str(out),
                "--timestamp",
            ],
        )

    assert result.exit_code == 0, result.output
    assert captured_urls == ["https://env-tsa.example.com/ts"]


def test_no_timestamp_flag_skips_tsa(tmp_path: Path) -> None:
    """Without --timestamp, no TSA call is made and no manifest.dsse.tsr present."""
    capsule_dir = _make_capsule(tmp_path)
    key_path = _make_keypair(tmp_path)
    out = tmp_path / "evidence.zip"

    with patch("novafabric.trust._rfc3161.httpx.post") as mock_post:
        result = runner.invoke(
            app,
            ["export-evidence", str(capsule_dir), "--key", str(key_path), "--output", str(out)],
        )

    assert result.exit_code == 0
    mock_post.assert_not_called()

    with zipfile.ZipFile(out) as zf:
        assert "manifest.dsse.tsr" not in zf.namelist()
