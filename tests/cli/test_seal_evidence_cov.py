# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Coverage-focused CLI tests for seal_propose.py and export_evidence.py.

Exercises error paths and success branches that were previously uncovered:
missing/nonexistent input files, malformed input, not-found entities, bad
args, and the success paths that build outputs. All tests are deterministic:
no real network, no sleeps, no external services. The only mocking is for the
genuinely-external sigstore import and the active EventRecorder hook.
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

from novafabric.cli.main import app
from novafabric.cli.seal_propose import seal_app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


def _gen_key_cert(tmp_path: Path, cn: str = "TestOperator") -> tuple[Path, Path]:
    """Generate an ECDSA P-256 key + self-signed cert; return (key, cert) paths."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_path = tmp_path / f"{cn}.key"
    key_path.write_bytes(key_pem)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"{cn}.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def _seed_policy(db_path: Path, key_path: Path, cert_path: Path) -> None:
    """Seed a signed promotion policy into the PolicyStore at db_path."""
    from novafabric.promote.policy_store import PolicyStore
    from novafabric.promote.predicates import (
        POLICY_PAYLOAD_TYPE,
        build_policy_predicate,
        sign_promote_envelope,
    )

    store = PolicyStore(db_path)
    predicate = build_policy_predicate(
        proposer_key_ids=["proposer"],
        approver_key_ids=["approver"],
    )
    payload = json.dumps(predicate).encode()
    envelope = sign_promote_envelope(payload, POLICY_PAYLOAD_TYPE, key_path, cert_path)
    store.put(envelope.decode("utf-8"))


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    """Minimal capsule directory with a capsule.yaml."""
    d = tmp_path / "cap-cov-001"
    d.mkdir()
    yaml.dump(
        {"run_id": "run-cov-001", "schema_version": "1.0.0"},
        (d / "capsule.yaml").open("w"),
    )
    return d


# ===========================================================================
# seal_propose.py — nova seal sign
# ===========================================================================


class TestSealSign:
    def test_sign_local_backend_delegates_and_exits_0(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"run_id": "r1"}))
        result = runner.invoke(seal_app, ["sign", str(manifest)])
        assert result.exit_code == 0, result.output
        assert "propose" in result.output.lower()

    def test_sign_inline_json_local(self, tmp_path: Path) -> None:
        result = runner.invoke(seal_app, ["sign", '{"run_id": "inline"}'])
        assert result.exit_code == 0, result.output

    def test_sign_invalid_path_and_invalid_json_exits_1(self) -> None:
        result = runner.invoke(seal_app, ["sign", "not a path and not json {"])
        assert result.exit_code == 1
        assert "valid" in result.output.lower()

    def test_sign_unknown_backend_exits_1(self, tmp_path: Path) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}")
        result = runner.invoke(
            seal_app, ["sign", str(manifest), "--backend", "bogus"]
        )
        assert result.exit_code == 1
        assert "backend" in result.output.lower()

    def test_sign_sigstore_backend_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mock the OIDC/network sign step; exercise store + identity extraction."""
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}")
        home = tmp_path / "nova-home"

        fake_bundle = {"mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3"}
        monkeypatch.setattr(
            "novafabric.trust.novaseal.sigstore_signer.SigstoreSigner.sign_artifact",
            lambda self, data: fake_bundle,
        )
        result = runner.invoke(
            seal_app,
            [
                "sign",
                str(manifest),
                "--backend",
                "sigstore",
                "--capsule-id",
                "cap-sig-1",
                "--home",
                str(home),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Sigstore bundle stored" in result.output
        assert "cap-sig-1" in result.output

    def test_sign_sigstore_runtime_error_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}")

        def _boom(self: object, data: bytes) -> dict:
            raise RuntimeError("no OIDC token available")

        monkeypatch.setattr(
            "novafabric.trust.novaseal.sigstore_signer.SigstoreSigner.sign_artifact",
            _boom,
        )
        result = runner.invoke(
            seal_app,
            ["sign", str(manifest), "--backend", "sigstore", "--home", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "signing failed" in result.output.lower()

    def test_sign_sigstore_backend_missing_dep_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the sigstore package is absent, the command fails cleanly."""
        import builtins

        manifest = tmp_path / "manifest.json"
        manifest.write_text("{}")

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object):
            if name == "sigstore":
                raise ImportError("no sigstore")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        result = runner.invoke(
            seal_app, ["sign", str(manifest), "--backend", "sigstore"]
        )
        assert result.exit_code == 1
        assert "sigstore" in result.output.lower()


# ===========================================================================
# seal_propose.py — nova seal propose
# ===========================================================================


class TestSealPropose:
    def test_propose_short_justification_exits_1(self, tmp_path: Path) -> None:
        key_path, cert_path = _gen_key_cert(tmp_path)
        result = runner.invoke(
            seal_app,
            [
                "propose",
                "cap-x",
                "--justification",
                "too short",
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--db",
                str(tmp_path / "merkle.db"),
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        assert result.exit_code == 1
        assert "20 characters" in result.output

    def test_propose_no_policy_exits_1(self, tmp_path: Path) -> None:
        key_path, cert_path = _gen_key_cert(tmp_path)
        result = runner.invoke(
            seal_app,
            [
                "propose",
                "cap-x",
                "--justification",
                "This is a long enough justification string.",
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--db",
                str(tmp_path / "empty.db"),
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        assert result.exit_code == 1
        assert "policy" in result.output.lower()

    def test_propose_success(self, tmp_path: Path) -> None:
        key_path, cert_path = _gen_key_cert(tmp_path, "Alice")
        db_path = tmp_path / "merkle.db"
        _seed_policy(db_path, key_path, cert_path)
        data_dir = tmp_path / "data"
        result = runner.invoke(
            seal_app,
            [
                "propose",
                "cap-success-1",
                "--justification",
                "Ready for production deployment after review.",
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--db",
                str(db_path),
                "--data-dir",
                str(data_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Proposal created" in result.output
        assert "Alice" in result.output


# ===========================================================================
# seal_propose.py — nova seal approve
# ===========================================================================


class TestSealApprove:
    def test_approve_proposal_not_found_exits_1(self, tmp_path: Path) -> None:
        key_path, cert_path = _gen_key_cert(tmp_path)
        result = runner.invoke(
            seal_app,
            [
                "approve",
                "nonexistent-uuid",
                "--capsule-id",
                "cap-missing",
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def _make_proposal(self, tmp_path: Path) -> tuple[str, str, Path, Path, Path]:
        """Create a stored proposal; return (capsule_id, uuid, key, cert, data_dir)."""
        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.predicates import (
            PROPOSAL_PAYLOAD_TYPE,
            build_proposal_predicate,
            sign_promote_envelope,
        )

        key_path, cert_path = _gen_key_cert(tmp_path, "Proposer")
        import hashlib

        capsule_id = "cap-approve-1"
        digest = hashlib.sha256(capsule_id.encode()).hexdigest()
        predicate = build_proposal_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=digest,
            target_env="staging",
            justification="Ready for staging after manual review pass.",
            proposer_subject="Proposer",
            policy_version="1",
        )
        payload = json.dumps(predicate).encode()
        envelope = sign_promote_envelope(
            payload, PROPOSAL_PAYLOAD_TYPE, key_path, cert_path
        )
        data_dir = tmp_path / "data"
        store = PromoteBundleStore(data_dir)
        uuid = store.put_proposal(capsule_id, envelope)
        return capsule_id, uuid, key_path, cert_path, data_dir

    def test_approve_declined_at_confirm_exits_0(self, tmp_path: Path) -> None:
        capsule_id, uuid, key_path, cert_path, data_dir = self._make_proposal(tmp_path)
        approver_key, approver_cert = _gen_key_cert(tmp_path, "Bob")
        result = runner.invoke(
            seal_app,
            [
                "approve",
                uuid,
                "--capsule-id",
                capsule_id,
                "--key",
                str(approver_key),
                "--cert",
                str(approver_cert),
                "--data-dir",
                str(data_dir),
            ],
            input="n\n",
        )
        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()

    def test_approve_records_event_when_recorder_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a capture recorder is active, the approval is recorded (fail-open)."""
        from unittest.mock import MagicMock

        capsule_id, uuid, key_path, cert_path, data_dir = self._make_proposal(tmp_path)
        approver_key, approver_cert = _gen_key_cert(tmp_path, "Carol")

        rec = MagicMock()
        monkeypatch.setattr(
            "novafabric.capture.event_recorder.get_current_recorder",
            lambda: rec,
        )
        result = runner.invoke(
            seal_app,
            [
                "approve",
                uuid,
                "--capsule-id",
                capsule_id,
                "--key",
                str(approver_key),
                "--cert",
                str(approver_cert),
                "--data-dir",
                str(data_dir),
            ],
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        rec.record_human_approval.assert_called_once()

    def test_approve_confirmed_success(self, tmp_path: Path) -> None:
        capsule_id, uuid, key_path, cert_path, data_dir = self._make_proposal(tmp_path)
        approver_key, approver_cert = _gen_key_cert(tmp_path, "Bob")
        result = runner.invoke(
            seal_app,
            [
                "approve",
                uuid,
                "--capsule-id",
                capsule_id,
                "--key",
                str(approver_key),
                "--cert",
                str(approver_cert),
                "--data-dir",
                str(data_dir),
            ],
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        assert "Approval recorded" in result.output
        assert "Bob" in result.output


# ===========================================================================
# seal_propose.py — nova seal bypass
# ===========================================================================


_LONG_REASON = (
    "Emergency bypass for production incident INC-00123 affecting all users now."
)


class TestSealBypass:
    def test_bypass_short_reason_exits_1(self, tmp_path: Path) -> None:
        key_path, cert_path = _gen_key_cert(tmp_path)
        result = runner.invoke(
            seal_app,
            [
                "bypass",
                "cap-b",
                "--reason",
                "too short",
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        assert result.exit_code == 1
        assert "50 characters" in result.output

    def test_bypass_bad_duration_exits_1(self, tmp_path: Path) -> None:
        key_path, cert_path = _gen_key_cert(tmp_path)
        result = runner.invoke(
            seal_app,
            [
                "bypass",
                "cap-b",
                "--reason",
                _LONG_REASON,
                "--duration",
                "notaduration",
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        assert result.exit_code == 1
        assert "Invalid duration" in result.output

    def test_bypass_duration_too_long_exits_1(self, tmp_path: Path) -> None:
        key_path, cert_path = _gen_key_cert(tmp_path)
        result = runner.invoke(
            seal_app,
            [
                "bypass",
                "cap-b",
                "--reason",
                _LONG_REASON,
                "--duration",
                "9d",
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        assert result.exit_code == 1
        assert "168h" in result.output

    def test_bypass_records_event_when_recorder_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import MagicMock

        key_path, cert_path = _gen_key_cert(tmp_path, "BypassOp")
        rec = MagicMock()
        monkeypatch.setattr(
            "novafabric.capture.event_recorder.get_current_recorder",
            lambda: rec,
        )
        result = runner.invoke(
            seal_app,
            [
                "bypass",
                "cap-bypass-rec",
                "--reason",
                _LONG_REASON,
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        assert result.exit_code == 0, result.output
        rec.record_human_approval.assert_called_once()

    def test_bypass_success(self, tmp_path: Path) -> None:
        key_path, cert_path = _gen_key_cert(tmp_path, "Operator")
        data_dir = tmp_path / "data"
        result = runner.invoke(
            seal_app,
            [
                "bypass",
                "cap-bypass-ok",
                "--reason",
                _LONG_REASON,
                "--duration",
                "24h",
                "--key",
                str(key_path),
                "--cert",
                str(cert_path),
                "--notify",
                "oncall@example.com",
                "--data-dir",
                str(data_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Bypass created" in result.output
        assert "Operator" in result.output


# ===========================================================================
# seal_propose.py — nova seal verify
# ===========================================================================


class TestSealVerify:
    def test_verify_missing_proposal_nonzero(self, tmp_path: Path) -> None:
        """No proposal/approval bundles → SoD verification fails (nonzero)."""
        key_path, cert_path = _gen_key_cert(tmp_path, "Vfy")
        db_path = tmp_path / "merkle.db"
        _seed_policy(db_path, key_path, cert_path)
        result = runner.invoke(
            seal_app,
            [
                "verify",
                "cap-never-proposed",
                "--db",
                str(db_path),
                "--data-dir",
                str(tmp_path / "data"),
            ],
        )
        assert result.exit_code != 0
        assert "failed" in result.output.lower()

    def test_verify_bypass_passes(self, tmp_path: Path) -> None:
        """A valid active bypass makes verify pass and report the bypass."""
        import hashlib
        from datetime import UTC, datetime, timedelta

        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.predicates import (
            BYPASS_PAYLOAD_TYPE,
            build_bypass_predicate,
            sign_promote_envelope,
        )

        key_path, cert_path = _gen_key_cert(tmp_path, "BypassVfy")
        db_path = tmp_path / "merkle.db"
        _seed_policy(db_path, key_path, cert_path)
        data_dir = tmp_path / "data"

        capsule_id = "cap-bypass-verify"
        valid_until = (datetime.now(UTC) + timedelta(hours=12)).isoformat()
        digest = hashlib.sha256(capsule_id.encode()).hexdigest()
        predicate = build_bypass_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=digest,
            target_environment="production",
            bypass_reason=_LONG_REASON,
            bypass_authorized_by="BypassVfy",
            valid_until=valid_until,
            notification_sent_to=[],
        )
        envelope = sign_promote_envelope(
            json.dumps(predicate).encode(), BYPASS_PAYLOAD_TYPE, key_path, cert_path
        )
        PromoteBundleStore(data_dir).put_bypass(capsule_id, envelope, valid_until)

        result = runner.invoke(
            seal_app,
            [
                "verify",
                capsule_id,
                "--db",
                str(db_path),
                "--data-dir",
                str(data_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "passed" in result.output.lower()
        assert "bypass active" in result.output.lower()


# ===========================================================================
# seal_propose.py — nova seal log verify
# ===========================================================================


class TestSealLogVerify:
    def test_log_verify_empty_sqlite_ok(self, tmp_path: Path) -> None:
        db = tmp_path / "merkle.db"
        result = runner.invoke(seal_app, ["log", "verify", "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert "Consistency" in result.output

    def test_log_verify_consistency_proof_error_exits_2(self, tmp_path: Path) -> None:
        """Requesting a consistency proof on an empty log fails with exit code 2."""
        db = tmp_path / "merkle.db"
        result = runner.invoke(
            seal_app, ["log", "verify", "--db", str(db), "--consistency", "5"]
        )
        # Either the proof errors out (exit 2) or fails verification (exit 2).
        assert result.exit_code == 2

    def test_log_verify_consistency_proof_success(self, tmp_path: Path) -> None:
        """A populated log proves append-only extension from an earlier size."""
        from novafabric.trust.novaseal.merkle import open_merkle_log

        db = tmp_path / "merkle.db"
        log = open_merkle_log(db)
        for i in range(3):
            log.append({"seq": i, "data": f"leaf-{i}"})

        result = runner.invoke(
            seal_app, ["log", "verify", "--db", str(db), "--consistency", "1", "--verbose"]
        )
        assert result.exit_code == 0, result.output
        assert "Append-only proof" in result.output


# ===========================================================================
# seal_propose.py — nova seal ratchet
# ===========================================================================


class TestSealRatchet:
    @pytest.fixture(autouse=True)
    def _isolate_ratchet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_RATCHET_DIR", str(tmp_path / "ratchet"))

    def test_ratchet_init_then_rotate_then_status(self) -> None:
        r_init = runner.invoke(
            seal_app, ["ratchet", "init", "--node-id", "node-a"]
        )
        assert r_init.exit_code == 0, r_init.output
        assert "epoch" in r_init.output.lower()

        r_rot = runner.invoke(
            seal_app, ["ratchet", "rotate", "--node-id", "node-a"]
        )
        assert r_rot.exit_code == 0, r_rot.output

        r_stat = runner.invoke(
            seal_app, ["ratchet", "status", "--node-id", "node-a"]
        )
        assert r_stat.exit_code == 0, r_stat.output
        assert "node-a" in r_stat.output

    def test_ratchet_status_unknown_node_exits_1(self) -> None:
        result = runner.invoke(
            seal_app, ["ratchet", "status", "--node-id", "never-existed"]
        )
        assert result.exit_code == 1

    def test_ratchet_rotate_unknown_node_exits_1(self) -> None:
        result = runner.invoke(
            seal_app, ["ratchet", "rotate", "--node-id", "never-existed"]
        )
        assert result.exit_code == 1

    def test_ratchet_init_twice_exits_1(self) -> None:
        first = runner.invoke(seal_app, ["ratchet", "init", "--node-id", "dup-node"])
        assert first.exit_code == 0, first.output
        second = runner.invoke(seal_app, ["ratchet", "init", "--node-id", "dup-node"])
        assert second.exit_code == 1


# ===========================================================================
# export_evidence.py — nova export-evidence (key-missing branch)
# ===========================================================================


def _make_full_capsule(tmp_path: Path) -> Path:
    """Run a trivial capture to produce a complete capsule directory."""
    import sys

    from novafabric.capture.orchestrator import CaptureOrchestrator

    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, "-c", "pass"])
    return result.capsule_dir


def _make_ed25519_key(tmp_path: Path) -> Path:
    from novafabric.evidence.signing import generate_keypair

    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    priv_path, _pub = generate_keypair(keys_dir)
    return priv_path


class TestExportEvidenceKey:
    def test_export_evidence_missing_key_exits_1(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "evidence.zip"
        result = runner.invoke(
            app, ["export-evidence", str(capsule_dir), "--output", str(out)]
        )
        assert result.exit_code == 1
        assert "--key is required" in result.output

    def test_export_evidence_bad_key_exits_1(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        bad_key = tmp_path / "bad.key"
        bad_key.write_text("not a real pem key")
        out = tmp_path / "evidence.zip"
        result = runner.invoke(
            app,
            [
                "export-evidence",
                str(capsule_dir),
                "--key",
                str(bad_key),
                "--output",
                str(out),
            ],
        )
        assert result.exit_code == 1
        assert "signing key" in result.output.lower()

    def test_export_evidence_success(self, tmp_path: Path) -> None:
        capsule = _make_full_capsule(tmp_path)
        key_path = _make_ed25519_key(tmp_path)
        out = tmp_path / "evidence.zip"
        result = runner.invoke(
            app,
            [
                "export-evidence",
                str(capsule),
                "--key",
                str(key_path),
                "--output",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert "evidence bundle written" in result.output

    def test_export_evidence_unsafe_skips_blocks_then_allows(
        self, tmp_path: Path
    ) -> None:
        """unsafe_skips in redaction-proof blocks (exit 2) unless overridden."""
        capsule = _make_full_capsule(tmp_path)
        proof_path = capsule / "redaction-proof.json"
        proof = json.loads(proof_path.read_text())
        proof.setdefault("unsafe_skips", []).append(
            {
                "finding_id": "01HXAY7M5JZ8R7K4P9DPBYK2WX",
                "rule_id": "synthetic",
                "rationale": "test",
                "decided_by": "cli",
                "decided_at": "2026-05-08T12:00:00Z",
            }
        )
        proof_path.write_text(json.dumps(proof, indent=2))
        key_path = _make_ed25519_key(tmp_path)
        out = tmp_path / "evidence.zip"

        blocked = runner.invoke(
            app,
            [
                "export-evidence",
                str(capsule),
                "--key",
                str(key_path),
                "--output",
                str(out),
            ],
        )
        assert blocked.exit_code == 2
        assert "unsafe_skips" in blocked.output

        allowed = runner.invoke(
            app,
            [
                "export-evidence",
                str(capsule),
                "--key",
                str(key_path),
                "--output",
                str(out),
                "--allow-unsafe-skips",
            ],
        )
        assert allowed.exit_code == 0, allowed.output

    def test_export_evidence_missing_capsule_file_exits_1(
        self, tmp_path: Path
    ) -> None:
        """A capsule missing a required file triggers CapsuleValidationError (exit 1)."""
        capsule = _make_full_capsule(tmp_path)
        (capsule / "trace.jsonl").unlink()
        key_path = _make_ed25519_key(tmp_path)
        out = tmp_path / "evidence.zip"
        result = runner.invoke(
            app,
            [
                "export-evidence",
                str(capsule),
                "--key",
                str(key_path),
                "--output",
                str(out),
            ],
        )
        assert result.exit_code == 1

    def test_export_evidence_timestamp_optional_continues_on_failure(
        self, tmp_path: Path
    ) -> None:
        """--timestamp-optional: a TSA transport failure is recorded but not fatal."""
        from unittest.mock import patch

        import httpx as httpx_mod

        capsule = _make_full_capsule(tmp_path)
        key_path = _make_ed25519_key(tmp_path)
        out = tmp_path / "evidence.zip"

        with patch(
            "novafabric.trust._rfc3161.httpx.post",
            side_effect=httpx_mod.TransportError("connection refused"),
        ):
            result = runner.invoke(
                app,
                [
                    "export-evidence",
                    str(capsule),
                    "--key",
                    str(key_path),
                    "--output",
                    str(out),
                    "--timestamp",
                    "--timestamp-optional",
                    "--timestamp-url",
                    "https://tsa.example.com/tsr",
                ],
            )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_export_evidence_timestamp_fatal_failure_exits_1(
        self, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        import httpx as httpx_mod

        capsule = _make_full_capsule(tmp_path)
        key_path = _make_ed25519_key(tmp_path)
        out = tmp_path / "evidence.zip"

        with patch(
            "novafabric.trust._rfc3161.httpx.post",
            side_effect=httpx_mod.TransportError("timeout"),
        ):
            result = runner.invoke(
                app,
                [
                    "export-evidence",
                    str(capsule),
                    "--key",
                    str(key_path),
                    "--output",
                    str(out),
                    "--timestamp",
                    "--timestamp-url",
                    "https://tsa.example.com/tsr",
                ],
            )
        assert result.exit_code == 1
        assert "timestamp" in result.output.lower()

    def test_export_evidence_timestamp_success_resolves_url(
        self, tmp_path: Path
    ) -> None:
        """A granted TSR adds manifest.dsse.tsr; exercises _add_timestamp_to_bundle."""
        from unittest.mock import MagicMock, patch

        from novafabric.trust._rfc3161 import _der_integer, _der_sequence

        capsule = _make_full_capsule(tmp_path)
        key_path = _make_ed25519_key(tmp_path)
        out = tmp_path / "evidence.zip"

        tsr_der = _der_sequence(_der_sequence(_der_integer(0)))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = tsr_der

        with patch(
            "novafabric.trust._rfc3161.httpx.post", return_value=mock_resp
        ):
            result = runner.invoke(
                app,
                [
                    "export-evidence",
                    str(capsule),
                    "--key",
                    str(key_path),
                    "--output",
                    str(out),
                    "--timestamp",
                    "--timestamp-url",
                    "https://tsa.example.com/tsr",
                ],
            )
        assert result.exit_code == 0, result.output
        import zipfile

        with zipfile.ZipFile(out) as zf:
            assert "manifest.dsse.tsr" in zf.namelist()

    def test_export_evidence_sigstore_flag_skips_without_rekor_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOVA_REKOR_URL", raising=False)
        capsule = _make_full_capsule(tmp_path)
        key_path = _make_ed25519_key(tmp_path)
        out = tmp_path / "evidence.zip"
        result = runner.invoke(
            app,
            [
                "export-evidence",
                str(capsule),
                "--key",
                str(key_path),
                "--output",
                str(out),
                "--sigstore",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "rekor" in result.output.lower()

    def test_export_evidence_sigstore_rekor_publish_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mock Rekor publish to return a UUID; exercise the success branch."""
        capsule = _make_full_capsule(tmp_path)
        key_path = _make_ed25519_key(tmp_path)
        out = tmp_path / "evidence.zip"
        monkeypatch.setattr(
            "novafabric.promote.rekor_client.maybe_publish",
            lambda envelope: "rekor-entry-12345",
        )
        result = runner.invoke(
            app,
            [
                "export-evidence",
                str(capsule),
                "--key",
                str(key_path),
                "--output",
                str(out),
                "--sigstore",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "rekor-entry-12345" in result.output


class TestExportHIPAAProof:
    def test_hipaa_default_output_inside_capsule(self, capsule_dir: Path) -> None:
        result = runner.invoke(app, ["export-hipaa-proof", str(capsule_dir)])
        assert result.exit_code == 0, result.output
        assert (capsule_dir / "hipaa-proof.json").exists()
        assert "HIPAA Safe Harbor proof written" in result.output

    def test_hipaa_explicit_output(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "hipaa.json"
        result = runner.invoke(
            app, ["export-hipaa-proof", str(capsule_dir), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_hipaa_write_failure_exits_1(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        """Output path that is an existing directory triggers the write-error path."""
        out_dir = tmp_path / "is-a-dir"
        out_dir.mkdir()
        result = runner.invoke(
            app, ["export-hipaa-proof", str(capsule_dir), "--output", str(out_dir)]
        )
        assert result.exit_code == 1
        assert "✗" in result.output


# ===========================================================================
# export_evidence.py — compliance exporters (annex-iv, nis2, ropa, aibom, rmf)
# ===========================================================================


class TestExportEvidenceHelpers:
    def test_resolve_tsa_url_cli_wins(self) -> None:
        from novafabric.cli.export_evidence import _resolve_tsa_url

        assert _resolve_tsa_url("https://cli.example/ts") == "https://cli.example/ts"

    def test_resolve_tsa_url_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.cli import export_evidence as mod

        cfg = tmp_path / "config.yaml"
        cfg.write_text("evidence:\n  tsa_url: https://config.example/ts\n")
        monkeypatch.setattr(mod, "_CONFIG_FILE", cfg)
        monkeypatch.delenv("NOVAFABRIC_TSA_URL", raising=False)
        assert mod._resolve_tsa_url(None) == "https://config.example/ts"

    def test_resolve_tsa_url_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.cli import export_evidence as mod

        monkeypatch.setattr(mod, "_CONFIG_FILE", tmp_path / "absent.yaml")
        monkeypatch.setenv("NOVAFABRIC_TSA_URL", "https://env.example/ts")
        assert mod._resolve_tsa_url(None) == "https://env.example/ts"

    def test_resolve_tsa_url_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.cli import export_evidence as mod
        from novafabric.trust._rfc3161 import DEFAULT_TSA_URL

        monkeypatch.setattr(mod, "_CONFIG_FILE", tmp_path / "absent.yaml")
        monkeypatch.delenv("NOVAFABRIC_TSA_URL", raising=False)
        assert mod._resolve_tsa_url(None) == DEFAULT_TSA_URL

    def test_add_timestamp_missing_dsse_returns_early(self, tmp_path: Path) -> None:
        """A bundle ZIP without the DSSE envelope prints a warning and returns."""
        import zipfile

        from novafabric.cli.export_evidence import _add_timestamp_to_bundle

        bundle = tmp_path / "bundle.zip"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("manifest.json", "{}")
        # Should not raise and should not modify the bundle.
        _add_timestamp_to_bundle(bundle, "https://tsa.example/ts", timestamp_optional=False)
        with zipfile.ZipFile(bundle) as zf:
            assert "manifest.dsse.tsr" not in zf.namelist()

    def test_rewrite_zip_entry_roundtrip(self, tmp_path: Path) -> None:
        import zipfile

        from novafabric.cli.export_evidence import _rewrite_zip_entry

        bundle = tmp_path / "b.zip"
        with zipfile.ZipFile(bundle, "w") as zf:
            zf.writestr("a.txt", "old")
            zf.writestr("b.txt", "keep")
        _rewrite_zip_entry(bundle, "a.txt", b"new")
        with zipfile.ZipFile(bundle) as zf:
            assert zf.read("a.txt") == b"new"
            assert zf.read("b.txt") == b"keep"


class TestExportAnnexIV:
    def test_annex_iv_success(self, capsule_dir: Path, tmp_path: Path) -> None:
        out_dir = tmp_path / "out"
        result = runner.invoke(
            app,
            [
                "export-annex-iv",
                str(capsule_dir),
                "--output-dir",
                str(out_dir),
                "--deployment-id",
                "dep-001",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Annex IV document written" in result.output
        produced = list(out_dir.glob("annex-iv-*.jsonld"))
        assert produced, f"no jsonld produced in {out_dir}"

    def test_annex_iv_export_failure_exits_1(self, tmp_path: Path) -> None:
        """An exporter exception path yields exit code 1."""
        capsule = tmp_path / "broken-cap"
        capsule.mkdir()
        # A directory named capsule.yaml makes the exporter raise on read.
        (capsule / "capsule.yaml").mkdir()
        out_dir = tmp_path / "out"
        result = runner.invoke(
            app,
            [
                "export-annex-iv",
                str(capsule),
                "--output-dir",
                str(out_dir),
                "--deployment-id",
                "dep-002",
            ],
        )
        assert result.exit_code == 1
        assert "✗" in result.output


class TestExportNIS2:
    def test_nis2_success(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "incident.json"
        result = runner.invoke(
            app,
            [
                "export-nis2",
                str(capsule_dir),
                "--output",
                str(out),
                "--incident-id",
                "INC-1",
            ],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert "NIS2 Phase 1 report written" in result.output

    def test_nis2_phase_2(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "incident.json"
        result = runner.invoke(
            app,
            [
                "export-nis2",
                str(capsule_dir),
                "--output",
                str(out),
                "--incident-id",
                "INC-2",
                "--phase",
                "2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_nis2_export_failure_exits_1(self, tmp_path: Path) -> None:
        capsule = tmp_path / "broken-cap"
        capsule.mkdir()
        (capsule / "capsule.yaml").mkdir()
        out = tmp_path / "incident.json"
        result = runner.invoke(
            app,
            [
                "export-nis2",
                str(capsule),
                "--output",
                str(out),
                "--incident-id",
                "INC-3",
            ],
        )
        assert result.exit_code == 1
        assert "✗" in result.output


class TestExportRoPA:
    def test_ropa_success_with_controller(
        self, capsule_dir: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "ropa.json"
        result = runner.invoke(
            app,
            [
                "export-ropa",
                str(capsule_dir),
                "--output",
                str(out),
                "--controller-name",
                "Acme Corp",
                "--controller-contact",
                "dpo@acme.example",
            ],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert "GDPR RoPA written" in result.output

    def test_ropa_export_failure_exits_1(self, tmp_path: Path) -> None:
        capsule = tmp_path / "broken-cap"
        capsule.mkdir()
        (capsule / "capsule.yaml").mkdir()
        out = tmp_path / "ropa.json"
        result = runner.invoke(
            app,
            ["export-ropa", str(capsule), "--output", str(out)],
        )
        assert result.exit_code == 1
        assert "✗" in result.output


class TestExportAIBOM:
    def test_aibom_default_output_inside_capsule(self, capsule_dir: Path) -> None:
        result = runner.invoke(app, ["export-aibom", str(capsule_dir)])
        assert result.exit_code == 0, result.output
        assert (capsule_dir / "aibom.json").exists()
        assert "AI-SBOM" in result.output

    def test_aibom_explicit_output(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "my-aibom.json"
        result = runner.invoke(
            app, ["export-aibom", str(capsule_dir), "--output", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_aibom_export_failure_exits_1(self, tmp_path: Path) -> None:
        capsule = tmp_path / "broken-cap"
        capsule.mkdir()
        (capsule / "capsule.yaml").mkdir()
        result = runner.invoke(app, ["export-aibom", str(capsule)])
        assert result.exit_code == 1
        assert "✗" in result.output


class TestExportNISTRMF:
    def test_nist_rmf_success(self, capsule_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "nist-rmf.json"
        result = runner.invoke(
            app,
            ["export-nist-rmf", str(capsule_dir), "--output", str(out)],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
        assert "NIST AI RMF report written" in result.output

    def test_nist_rmf_export_failure_exits_1(self, tmp_path: Path) -> None:
        capsule = tmp_path / "broken-cap"
        capsule.mkdir()
        (capsule / "capsule.yaml").mkdir()
        out = tmp_path / "nist-rmf.json"
        result = runner.invoke(
            app,
            ["export-nist-rmf", str(capsule), "--output", str(out)],
        )
        assert result.exit_code == 1
        assert "✗" in result.output
