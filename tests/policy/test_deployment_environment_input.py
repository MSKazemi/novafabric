"""ADR-0126 P3 — ``deployment_environment`` in the ADR-0019 policy input document.

Acceptance criteria:

- ``PolicyResource.deployment_environment`` is additive + optional (default
  ``None`` → Rego ``null``); existing inputs validate unchanged;
- the value is read verbatim from the capsule's typed top-level field —
  never inferred, never taken from free-form ``metadata``, never fabricated;
  a missing/unreadable/malformed manifest or an out-of-rule value yields
  ``None`` without raising (fail-safe);
- the evidence-export gate passes the recorded value to the engine;
- an example policy can condition on it (production-only requirement) — the
  Rego suite runs when the ``opa`` binary is available.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from novafabric.policy import (
    OpaEngine,
    PolicyDecision,
    PolicyInput,
    PolicyResource,
    PolicySubject,
    deployment_environment_from_capsule,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "policy-environment"
_EXAMPLE_POLICY = _FIXTURES / "production_export_gate.rego"
_OPA_MISSING = shutil.which("opa") is None


def _manifest_capsule(tmp_path: Path, manifest: object) -> Path:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return capsule


def _export_input(env: str | None, *, unsafe_skips: int = 0) -> PolicyInput:
    return PolicyInput(
        action="evidence_export",
        subject=PolicySubject(user="alice"),
        resource=PolicyResource(
            kind="capsule",
            ref="run-1",
            redaction_proof_present=True,
            unsafe_skips=unsafe_skips,
            deployment_environment=env,
        ),
    )


# ── model: additive + optional ──────────────────────────────────────────────


def test_field_defaults_to_none_and_serializes_as_null() -> None:
    resource = PolicyResource(kind="asset", ref="a@1")
    assert resource.deployment_environment is None
    payload = json.loads(
        PolicyInput(
            action="promote", subject=PolicySubject(user="u"), resource=resource
        ).model_dump_json()
    )
    assert payload["resource"]["deployment_environment"] is None


def test_field_roundtrips_verbatim() -> None:
    inp = _export_input("prod-eu")
    restored = PolicyInput.model_validate_json(inp.model_dump_json())
    assert restored.resource.deployment_environment == "prod-eu"


def test_legacy_input_without_field_still_validates() -> None:
    legacy = {
        "action": "evidence_export",
        "subject": {"user": "u"},
        "resource": {"kind": "capsule", "ref": "r"},
    }
    assert PolicyInput.model_validate(legacy).resource.deployment_environment is None


# ── reading the recorded value ──────────────────────────────────────────────


@pytest.mark.parametrize("env", ["production", "staging", "Production", "prod-eu:1"])
def test_reads_recorded_value_verbatim(tmp_path: Path, env: str) -> None:
    capsule = _manifest_capsule(
        tmp_path, {"run_id": "r", "deployment_environment": env, "environment_source": "cli-flag"}
    )
    assert deployment_environment_from_capsule(capsule) == env


def test_absent_field_is_none_not_fabricated(tmp_path: Path) -> None:
    assert deployment_environment_from_capsule(_manifest_capsule(tmp_path, {"run_id": "r"})) is None


def test_metadata_is_not_consulted(tmp_path: Path) -> None:
    capsule = _manifest_capsule(
        tmp_path, {"run_id": "r", "metadata": {"deployment_environment": "production"}}
    )
    assert deployment_environment_from_capsule(capsule) is None


@pytest.mark.parametrize("bad", ["", "prod env", "x" * 65, 7, ["production"]])
def test_out_of_rule_value_is_dropped(tmp_path: Path, bad: object) -> None:
    capsule = _manifest_capsule(tmp_path, {"run_id": "r", "deployment_environment": bad})
    assert deployment_environment_from_capsule(capsule) is None


def test_missing_manifest_is_none(tmp_path: Path) -> None:
    assert deployment_environment_from_capsule(tmp_path) is None


def test_unparseable_manifest_is_none(tmp_path: Path) -> None:
    (tmp_path / "capsule.yaml").write_text("run_id: [unclosed\n", encoding="utf-8")
    assert deployment_environment_from_capsule(tmp_path) is None


def test_non_mapping_manifest_is_none(tmp_path: Path) -> None:
    assert deployment_environment_from_capsule(_manifest_capsule(tmp_path, ["a", "b"])) is None


# ── evidence-export gate wiring ─────────────────────────────────────────────


def _allow_engine() -> MagicMock:
    engine = MagicMock()
    engine.evaluate.return_value = PolicyDecision(allow=True, decision_id="d-1")
    return engine


@pytest.mark.parametrize("env", ["production", None])
def test_export_gate_passes_recorded_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str | None
) -> None:
    from novafabric.capture.orchestrator import CaptureOrchestrator
    from novafabric.evidence.bundle import EvidenceBundleBuilder
    from novafabric.evidence.signing import LocalSigner, generate_keypair

    monkeypatch.setattr("novafabric.evidence.bundle.AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.delenv("NOVAFABRIC_ENVIRONMENT", raising=False)
    result = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        command=[sys.executable, "-c", "pass"], environment=env
    )
    keys = tmp_path / "keys"
    keys.mkdir()
    private_key, _ = generate_keypair(keys)
    engine = _allow_engine()

    with patch("novafabric.evidence.bundle.get_policy_engine", return_value=engine):
        EvidenceBundleBuilder(
            capsule_dir=result.capsule_dir,
            signer=LocalSigner(private_key),
            output_path=tmp_path / "evidence.zip",
            actor="tester",
        ).build()

    sent: PolicyInput = engine.evaluate.call_args[0][0]
    assert sent.action == "evidence_export"
    assert sent.resource.deployment_environment == env


# ── example environment-conditioned policy ──────────────────────────────────


def test_example_policy_conditions_on_environment() -> None:
    source = _EXAMPLE_POLICY.read_text(encoding="utf-8")
    assert 'input.resource.deployment_environment == "production"' in source
    assert (_FIXTURES / "production_export_gate_test.rego").is_file()


def test_custom_policy_receives_environment_in_input() -> None:
    """The OPA subprocess sees ``input.resource.deployment_environment``."""
    completed = MagicMock(
        returncode=0,
        stdout=json.dumps({"result": [{"expressions": [{"value": True}]}]}),
        stderr="",
    )
    with patch("subprocess.run", return_value=completed) as run:
        decision = OpaEngine().evaluate(
            _export_input("production"),
            policy_source=_EXAMPLE_POLICY.read_text(encoding="utf-8"),
        )
    assert decision.allow is True
    sent = json.loads(run.call_args.kwargs["input"])
    assert sent["resource"]["deployment_environment"] == "production"
    assert run.call_args.args[0][-1] == "data.novafabric.examples.production_export.allow"


@pytest.mark.skipif(_OPA_MISSING, reason="opa binary not on PATH")
def test_example_policy_rego_suite_passes() -> None:
    result = subprocess.run(
        ["opa", "test", str(_FIXTURES), "--verbose"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(_OPA_MISSING, reason="opa binary not on PATH")
@pytest.mark.parametrize(
    ("env", "unsafe_skips", "expected"),
    [
        ("production", 0, True),
        ("production", 2, False),
        ("staging", 2, True),
        (None, 2, True),
    ],
)
def test_example_policy_end_to_end(env: str | None, unsafe_skips: int, expected: bool) -> None:
    decision = OpaEngine().evaluate(
        _export_input(env, unsafe_skips=unsafe_skips),
        policy_source=_EXAMPLE_POLICY.read_text(encoding="utf-8"),
    )
    assert decision.allow is expected
