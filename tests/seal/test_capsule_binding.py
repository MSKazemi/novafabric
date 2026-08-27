"""ADR-0251 — the seal must be bound to the capsule directory it sits in.

Every test here perturbs the directory *after* sealing. That is the whole point:
the three original checks (signature, timestamp, Merkle inclusion) were each
individually correct and individually tested, and a forged capsule still verified
green — because no test ever modified the directory between seal and verify.

Measured on 2026-08-27 against today's code before the fix:

    appended a fabricated model call to model-calls.jsonl   -> EXIT 0, three green
    edited `status: success` -> `success-FORGED` in capsule.yaml -> EXIT 0, three green
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture.orchestrator import _evidence_digests
from novafabric.cli.main import app
from novafabric.trust.novaseal import KeyConfig, NovaSeal

runner = CliRunner()


@pytest.fixture()
def bound_capsule(tmp_path):
    """A capsule sealed the way `nova capture` seals one, with evidence_digests.

    Returns ``(capsule_dir, config_path)``.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ADR-0251-Test")])
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

    merkle_db = tmp_path / "merkle.db"
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {merkle_db}\n"
    )

    capsule_dir = tmp_path / "capsule-adr0251"
    (capsule_dir / "outputs").mkdir(parents=True)
    (capsule_dir / "model-calls.jsonl").write_text(
        '{"model":"gpt-4o","tokens_in":77,"tokens_out":12}\n'
    )
    (capsule_dir / "trace.jsonl").write_text('{"span":"root"}\n')
    (capsule_dir / "env.lock").write_text("python=3.13\n")
    (capsule_dir / "outputs" / "stdout.txt").write_text("hello\n")

    manifest = {
        "run_id": "adr0251-test",
        "status": "success",
        "model_calls_ref": "model-calls.jsonl",
        "trace_ref": "trace.jsonl",
        "environment_ref": "env.lock",
    }
    manifest["evidence_digests"] = _evidence_digests(capsule_dir)
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest, allow_unicode=True))

    seal = NovaSeal(
        config=KeyConfig(
            profile="local", key_path=str(key_path), cert_path=str(cert_path)
        ),
        tsa_url="",
        db_path=str(merkle_db),
    )
    bundle = seal.seal(manifest)
    seal_dir = capsule_dir / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry, indent=2))
    return capsule_dir, config_path


def _verify(capsule_dir, config_path):
    return runner.invoke(
        app, ["verify", str(capsule_dir), "--seal-config", str(config_path)]
    )


class TestEvidenceDigests:
    """The digest map itself."""

    def test_covers_every_file_except_the_two_it_cannot(self, bound_capsule):
        capsule_dir, _ = bound_capsule
        digests = _evidence_digests(capsule_dir)
        assert "capsule.yaml" not in digests, "the map cannot hash its own carrier"
        assert not any(k.startswith(".seal/") for k in digests)
        assert set(digests) == {
            "env.lock",
            "model-calls.jsonl",
            "outputs/stdout.txt",
            "trace.jsonl",
        }

    def test_entry_shape_matches_the_evidence_bundle(self, bound_capsule):
        capsule_dir, _ = bound_capsule
        entry = _evidence_digests(capsule_dir)["trace.jsonl"]
        assert set(entry) == {"sha256", "size_bytes"}
        assert entry["sha256"].startswith("sha256:")
        assert len(entry["sha256"]) == len("sha256:") + 64
        assert entry["size_bytes"] == (capsule_dir / "trace.jsonl").stat().st_size

    def test_digest_is_the_actual_content_hash(self, bound_capsule):
        capsule_dir, _ = bound_capsule
        raw = (capsule_dir / "model-calls.jsonl").read_bytes()
        expected = "sha256:" + hashlib.sha256(raw).hexdigest()
        assert _evidence_digests(capsule_dir)["model-calls.jsonl"]["sha256"] == expected

    def test_keys_are_sorted_so_the_signed_payload_is_stable(self, bound_capsule):
        capsule_dir, _ = bound_capsule
        keys = list(_evidence_digests(capsule_dir))
        assert keys == sorted(keys)


class TestUnmodifiedCapsuleStillVerifies:
    def test_baseline_is_green_and_exits_zero(self, bound_capsule):
        capsule_dir, config_path = bound_capsule
        result = _verify(capsule_dir, config_path)
        assert result.exit_code == 0, result.output
        assert "Manifest binding" in result.output
        assert "Evidence binding" in result.output
        assert "FAIL" not in result.output


class TestTamperIsDetected:
    """The regression guard. Each case exited 0 before ADR-0251."""

    def test_appending_a_fabricated_model_call_fails(self, bound_capsule):
        capsule_dir, config_path = bound_capsule
        with (capsule_dir / "model-calls.jsonl").open("a") as fh:
            fh.write('{"model":"gpt-4o","tokens_in":999999,"tokens_out":999999}\n')
        result = _verify(capsule_dir, config_path)
        assert result.exit_code == 1, result.output
        assert "modified:" in result.output
        assert "model-calls.jsonl" in result.output

    def test_editing_the_signed_manifest_on_disk_fails(self, bound_capsule):
        capsule_dir, config_path = bound_capsule
        manifest_file = capsule_dir / "capsule.yaml"
        doc = yaml.safe_load(manifest_file.read_text())
        doc["status"] = "success-FORGED"
        manifest_file.write_text(yaml.dump(doc, allow_unicode=True))
        result = _verify(capsule_dir, config_path)
        assert result.exit_code == 1, result.output
        assert "Manifest binding" in result.output
        assert "status" in result.output

    def test_deleting_an_evidence_file_fails(self, bound_capsule):
        capsule_dir, config_path = bound_capsule
        (capsule_dir / "trace.jsonl").unlink()
        result = _verify(capsule_dir, config_path)
        assert result.exit_code == 1, result.output
        assert "missing:" in result.output
        assert "trace.jsonl" in result.output

    def test_a_forged_log_entry_capsule_id_fails(self, bound_capsule):
        capsule_dir, config_path = bound_capsule
        log_file = capsule_dir / ".seal" / "log-entry.json"
        entry = json.loads(log_file.read_text())
        entry["entry"]["capsule_id"] = "0" * 64
        log_file.write_text(json.dumps(entry, indent=2))
        result = _verify(capsule_dir, config_path)
        assert result.exit_code == 1, result.output
        assert "capsule_id" in result.output

    def test_the_signed_payload_still_holds_the_original_value(self, bound_capsule):
        """The forgery is on disk only — the envelope is untouched and still true."""
        capsule_dir, _ = bound_capsule
        manifest_file = capsule_dir / "capsule.yaml"
        doc = yaml.safe_load(manifest_file.read_text())
        doc["status"] = "success-FORGED"
        manifest_file.write_text(yaml.dump(doc, allow_unicode=True))

        envelope = json.loads((capsule_dir / ".seal" / "manifest.dsse").read_bytes())
        signed = json.loads(base64.urlsafe_b64decode(envelope["payload"] + "=="))
        assert signed["status"] == "success"


class TestLegitimateAdditionsAreReportedNotFailed:
    def test_a_post_seal_artifact_is_surfaced_but_does_not_fail(self, bound_capsule):
        capsule_dir, config_path = bound_capsule
        (capsule_dir / "c2pa-manifest.json").write_text('{"c2pa.ai.generated": true}')
        result = _verify(capsule_dir, config_path)
        assert result.exit_code == 0, result.output
        assert "not covered by the seal" in result.output
        assert "c2pa-manifest.json" in result.output

    def test_it_is_never_silent(self, bound_capsule):
        """An uncovered file must be named. Silence would read as coverage."""
        capsule_dir, config_path = bound_capsule
        (capsule_dir / "cost-report.json").write_text("{}")
        assert "cost-report.json" in _verify(capsule_dir, config_path).output


class TestCapsulesSealedBeforeThisAdr:
    """Absent means absent. Never print OK for a check that did not run."""

    def test_missing_evidence_digests_reports_not_present_and_exits_zero(
        self, bound_capsule
    ):
        capsule_dir, config_path = bound_capsule
        # Re-seal without the digest block, as pre-ADR-0251 capture did.
        doc = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
        doc.pop("evidence_digests")
        (capsule_dir / "capsule.yaml").write_text(yaml.dump(doc, allow_unicode=True))

        profile = yaml.safe_load(config_path.read_text())
        seal = NovaSeal(
            config=KeyConfig(
                profile="local",
                key_path=str(profile["key_path"]),
                cert_path=str(profile["cert_path"]),
            ),
            tsa_url="",
            db_path=str(profile["merkle_db"]),
        )
        bundle = seal.seal(doc)
        seal_dir = capsule_dir / ".seal"
        (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
        (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry, indent=2))

        result = _verify(capsule_dir, config_path)
        assert result.exit_code == 0, result.output
        assert "NOT PRESENT" in result.output
        assert "Evidence binding (per-file sha256): OK" not in result.output

    def test_such_a_capsule_does_not_claim_evidence_binding(self, bound_capsule):
        """The honesty rule the RFC 3161 fix established, applied to a second check."""
        capsule_dir, config_path = bound_capsule
        doc = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
        doc.pop("evidence_digests")
        (capsule_dir / "capsule.yaml").write_text(yaml.dump(doc, allow_unicode=True))
        profile = yaml.safe_load(config_path.read_text())
        seal = NovaSeal(
            config=KeyConfig(
                profile="local",
                key_path=str(profile["key_path"]),
                cert_path=str(profile["cert_path"]),
            ),
            tsa_url="",
            db_path=str(profile["merkle_db"]),
        )
        bundle = seal.seal(doc)
        (capsule_dir / ".seal" / "manifest.dsse").write_bytes(bundle.dsse_envelope)
        (capsule_dir / ".seal" / "log-entry.json").write_text(
            json.dumps(bundle.log_entry, indent=2)
        )
        out = _verify(capsule_dir, config_path).output
        assert "sealed before evidence_digests" in out
