"""Tests for policy gates wired into promote_asset(), EvidenceBundleBuilder.build(),
and ReplayEngine.run() (allow_mutating path).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from novafabric.audit import AuditEventType
from novafabric.audit._models import AuditEntry
from novafabric.policy._models import PolicyDecision, PolicyDeniedError
from novafabric.registry.service import (
    PromotionBlockedError,
    promote_asset,
    register_asset,
)
from novafabric.replay._engine import ReplayEngine
from novafabric.replay._flags import ReplayFlags
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deny_decision(reason: str = "test-deny", decision_id: str = "dec-001") -> PolicyDecision:
    return PolicyDecision(allow=False, reason=reason, decision_id=decision_id)


def _allow_decision(reason: str = "ok", decision_id: str = "dec-002") -> PolicyDecision:
    return PolicyDecision(allow=True, reason=reason, decision_id=decision_id)


def _mock_engine(decision: PolicyDecision) -> MagicMock:
    engine = MagicMock()
    engine.evaluate.return_value = decision
    return engine


def _register_model(tmp_db: Path, fixtures_dir: Path) -> None:
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)


# ---------------------------------------------------------------------------
# promote_asset() — policy gate
# ---------------------------------------------------------------------------


class TestPromotePolicyGate:
    def test_promote_blocked_by_policy(
        self, tmp_db: Path, fixtures_dir: Path
    ) -> None:
        """Policy deny (force=False) → PromotionBlockedError raised."""
        _register_model(tmp_db, fixtures_dir)
        engine = _mock_engine(_deny_decision(reason="no-prod-access", decision_id="X1"))

        with patch("novafabric.registry.service.get_policy_engine", return_value=engine):
            with pytest.raises(PromotionBlockedError) as exc_info:
                promote_asset(
                    "fraud-model",
                    "1.0.0",
                    AssetStatus.staging,
                    actor="tester",
                    db_path=tmp_db,
                )

        msg = str(exc_info.value)
        assert "no-prod-access" in msg
        assert "X1" in msg

    def test_promote_force_bypasses_policy(
        self, tmp_db: Path, fixtures_dir: Path
    ) -> None:
        """force=True skips the policy block; promotion succeeds despite deny."""
        _register_model(tmp_db, fixtures_dir)
        engine = _mock_engine(_deny_decision())

        with patch("novafabric.registry.service.get_policy_engine", return_value=engine):
            result = promote_asset(
                "fraud-model",
                "1.0.0",
                AssetStatus.staging,
                actor="tester",
                force=True,
                db_path=tmp_db,
            )

        assert result["status"] == "staging"

    def test_promote_logs_policy_allow(
        self, tmp_db: Path, fixtures_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A policy.allow event is written to the audit log."""
        audit_path = tmp_path / "audit.jsonl"
        monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

        _register_model(tmp_db, fixtures_dir)
        engine = _mock_engine(_allow_decision(decision_id="dec-allow-1"))

        with patch("novafabric.registry.service.get_policy_engine", return_value=engine):
            promote_asset(
                "fraud-model",
                "1.0.0",
                AssetStatus.staging,
                actor="tester",
                db_path=tmp_db,
            )

        assert audit_path.exists(), "audit log was not written"
        entries = [
            AuditEntry.model_validate_json(line)
            for line in audit_path.read_text().splitlines()
            if line.strip()
        ]
        assert len(entries) == 1
        assert entries[0].event_type == AuditEventType.POLICY_ALLOW
        assert entries[0].actor == "tester"
        assert entries[0].details["action"] == "promote"
        assert entries[0].details["decision_id"] == "dec-allow-1"

    def test_promote_logs_policy_deny(
        self, tmp_db: Path, fixtures_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A policy.deny event is written to the audit log even when the promotion is blocked."""
        audit_path = tmp_path / "audit.jsonl"
        monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

        _register_model(tmp_db, fixtures_dir)
        engine = _mock_engine(_deny_decision(decision_id="dec-deny-1"))

        with patch("novafabric.registry.service.get_policy_engine", return_value=engine):
            with pytest.raises(PromotionBlockedError):
                promote_asset(
                    "fraud-model",
                    "1.0.0",
                    AssetStatus.staging,
                    actor="tester",
                    db_path=tmp_db,
                )

        entries = [
            AuditEntry.model_validate_json(line)
            for line in audit_path.read_text().splitlines()
            if line.strip()
        ]
        assert len(entries) == 1
        assert entries[0].event_type == AuditEventType.POLICY_DENY
        assert entries[0].details["decision_id"] == "dec-deny-1"

    def test_promote_force_still_logs_deny_event(
        self, tmp_db: Path, fixtures_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=True bypasses the raise but the deny is still logged for audit trail."""
        audit_path = tmp_path / "audit.jsonl"
        monkeypatch.setattr("novafabric.registry.service.AUDIT_LOG_PATH", audit_path)

        _register_model(tmp_db, fixtures_dir)
        engine = _mock_engine(_deny_decision(decision_id="dec-force-bypass"))

        with patch("novafabric.registry.service.get_policy_engine", return_value=engine):
            promote_asset(
                "fraud-model",
                "1.0.0",
                AssetStatus.staging,
                actor="tester",
                force=True,
                db_path=tmp_db,
            )

        entries = [
            AuditEntry.model_validate_json(line)
            for line in audit_path.read_text().splitlines()
            if line.strip()
        ]
        assert len(entries) == 1
        assert entries[0].event_type == AuditEventType.POLICY_DENY
        assert entries[0].details["decision_id"] == "dec-force-bypass"


# ---------------------------------------------------------------------------
# EvidenceBundleBuilder.build() — evidence_export gate
# ---------------------------------------------------------------------------


def _make_real_capsule(tmp_path: Path) -> Path:
    """Build a minimal but structurally valid capsule via CaptureOrchestrator."""
    from novafabric.capture.orchestrator import CaptureOrchestrator

    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, "-c", "pass"])
    return result.capsule_dir


def _make_keypair(tmp_path: Path) -> Path:
    from novafabric.evidence.signing import generate_keypair

    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    priv_path, _ = generate_keypair(keys_dir)
    return priv_path


class TestEvidenceExportPolicyGate:
    def test_export_blocked_by_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Policy deny raises PolicyDeniedError before any ZIP is written."""
        from novafabric.evidence.bundle import EvidenceBundleBuilder
        from novafabric.evidence.signing import LocalSigner

        monkeypatch.setattr("novafabric.evidence.bundle.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")

        capsule_dir = _make_real_capsule(tmp_path)
        signer = LocalSigner(_make_keypair(tmp_path))
        out = tmp_path / "evidence.zip"
        engine = _mock_engine(_deny_decision(reason="no-export", decision_id="E1"))

        with patch("novafabric.evidence.bundle.get_policy_engine", return_value=engine):
            with pytest.raises(PolicyDeniedError) as exc_info:
                EvidenceBundleBuilder(
                    capsule_dir=capsule_dir,
                    signer=signer,
                    output_path=out,
                    actor="tester",
                ).build()

        assert "no-export" in str(exc_info.value)
        assert not out.exists(), "ZIP must not be written when policy denies"

    def test_export_gate_called_on_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_policy_engine() is invoked exactly once when build() is called."""
        from novafabric.evidence.bundle import EvidenceBundleBuilder
        from novafabric.evidence.signing import LocalSigner

        monkeypatch.setattr("novafabric.evidence.bundle.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")

        capsule_dir = _make_real_capsule(tmp_path)
        signer = LocalSigner(_make_keypair(tmp_path))
        out = tmp_path / "evidence.zip"
        engine = _mock_engine(_allow_decision())

        with patch("novafabric.evidence.bundle.get_policy_engine", return_value=engine):
            EvidenceBundleBuilder(
                capsule_dir=capsule_dir,
                signer=signer,
                output_path=out,
                actor="tester",
            ).build()

        engine.evaluate.assert_called_once()
        call_inp = engine.evaluate.call_args[0][0]
        assert call_inp.action == "evidence_export"
        assert call_inp.subject.user == "tester"


# ---------------------------------------------------------------------------
# ReplayEngine.run() — replay_mutating gate
# ---------------------------------------------------------------------------


def _make_capsule(tmp_path: Path, run_id: str = "TESTRUN01") -> Path:
    cap = tmp_path / "capsules" / run_id
    cap.mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "outputs").mkdir()
    manifest = {
        "schema_version": "0.1.0",
        "run_id": run_id,
        "status": "success",
        "command": [sys.executable, "-c", "pass"],
        "capture_mode": "cli-wrapper",
        "novafabric_version": "0.2.0",
        "model_call_count": 0,
        "tool_call_count": 0,
    }
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "env.lock").write_text(yaml.dump({
        "schema_version": "0.1.0",
        "python": {"version": "3.12.3", "interpreter": "cpython"},
        "host": {"os": "linux", "arch": "x86_64"},
    }))
    (cap / "replay.yaml").write_text(yaml.dump({"schema_version": "0.1.0"}))
    (cap / "model-calls.jsonl").write_text("")
    (cap / "tool-calls.jsonl").write_text("")
    (cap / "trace.jsonl").write_text("")
    (cap / "redaction-proof.json").write_text(json.dumps({"findings": []}))
    return cap


class TestReplayMutatingGate:
    def test_mutating_replay_blocked_by_policy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Policy deny raises PolicyDeniedError for allow_mutating replay."""
        monkeypatch.setattr("novafabric.replay._engine.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")

        cap = _make_capsule(tmp_path)
        engine = _mock_engine(_deny_decision(reason="no-mutate", decision_id="R1"))
        flags = ReplayFlags(mode="mocked", allow_mutating=True)

        with patch("novafabric.replay._engine.get_policy_engine", return_value=engine):
            with pytest.raises(PolicyDeniedError) as exc_info:
                ReplayEngine(
                    capsule_dir=cap, flags=flags, base_dir=tmp_path / "replays", actor="tester"
                ).run()

        assert "no-mutate" in str(exc_info.value)
        assert "R1" in str(exc_info.value)

    def test_non_mutating_replay_skips_policy_gate(self, tmp_path: Path) -> None:
        """Standard (non-mutating) mocked replay does NOT consult the policy engine."""
        cap = _make_capsule(tmp_path)
        engine = _mock_engine(_deny_decision())
        flags = ReplayFlags(mode="mocked", allow_mutating=False)

        with patch("novafabric.replay._engine.get_policy_engine", return_value=engine):
            result = ReplayEngine(
                capsule_dir=cap, flags=flags, base_dir=tmp_path / "replays", actor="tester"
            ).run()

        engine.evaluate.assert_not_called()
        assert result is not None

    def test_forensic_replay_skips_policy_gate(self, tmp_path: Path) -> None:
        """Forensic mode never triggers the mutating gate."""
        cap = _make_capsule(tmp_path)
        engine = _mock_engine(_deny_decision())
        flags = ReplayFlags(mode="forensic", allow_mutating=False)

        with patch("novafabric.replay._engine.get_policy_engine", return_value=engine):
            result = ReplayEngine(
                capsule_dir=cap, flags=flags, base_dir=tmp_path / "replays", actor="tester"
            ).run()

        engine.evaluate.assert_not_called()
        assert result.mode == "forensic"

    def test_mutating_replay_gate_called_with_correct_action(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When allow_mutating=True, evaluate() is called with action='replay_mutating'."""
        monkeypatch.setattr("novafabric.replay._engine.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")

        cap = _make_capsule(tmp_path)
        engine = _mock_engine(_allow_decision())
        flags = ReplayFlags(mode="mocked", allow_mutating=True)

        with patch("novafabric.replay._engine.get_policy_engine", return_value=engine):
            ReplayEngine(
                capsule_dir=cap, flags=flags, base_dir=tmp_path / "replays", actor="tester"
            ).run()

        engine.evaluate.assert_called_once()
        call_inp = engine.evaluate.call_args[0][0]
        assert call_inp.action == "replay_mutating"
        assert call_inp.subject.user == "tester"
        assert call_inp.resource.ref == "TESTRUN01"
