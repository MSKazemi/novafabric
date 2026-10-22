"""An exact-mode re-performance attestation must not be signed for an ineligible run.

``--mode exact`` re-executes nothing: ``ReplayEngine._exact`` checks whether the
recorded run meets the exact-replay preconditions and reports the answer in
``exact_eligible``, with ``status="success"`` either way. ``verdict_for`` maps a
successful exact-mode result to ``match: exact``, so before this guard
``nova evidence attest-replay --mode exact`` signed ``match: exact`` for a run whose
own result said ``exact_eligible: false``. Session replay already refuses that case
(``session/replay.py``); the attestation path now refuses it too — nothing is signed
(ADR-0269 option B, "refuse to attest what was not performed").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.evidence import evidence_app
from novafabric.evidence.reperformance import (
    NotAttestableError,
    build_reperformance_attestation,
)
from novafabric.evidence.signing import generate_keypair
from novafabric.replay._result import ReplayResult

runner = CliRunner()


def _result(*, eligible: bool | None, reasons: list[str] | None = None) -> ReplayResult:
    return ReplayResult(
        replay_id="rp-x",
        replay_of_run_id="run-x",
        mode="exact",
        status="success",
        start_time="2026-10-22T00:00:00Z",
        end_time="2026-10-22T00:00:01Z",
        duration_ms=1000,
        policy_flags_used=["--mode=exact"],
        env_warnings=[],
        exact_eligible=eligible,
        exact_reasons=reasons,
    )


def _capsule(tmp_path: Path, *, lock_mode: str) -> Path:
    capsule = tmp_path / "run-exact"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        yaml.dump({"run_id": "run-exact", "command": ["python", "agent.py"]})
    )
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"gen_ai.request.model": "m", "gen_ai.request.seed": 7}) + "\n"
    )
    (capsule / "env.lock").write_text(yaml.dump({"mode": lock_mode}))
    return capsule


@pytest.mark.parametrize("eligible", [False, None])
def test_builder_refuses_an_exact_result_that_is_not_eligible(
    tmp_path: Path, eligible: bool | None
) -> None:
    capsule = _capsule(tmp_path, lock_mode="best-effort")
    with pytest.raises(NotAttestableError, match="not eligible"):
        build_reperformance_attestation(
            capsule, _result(eligible=eligible, reasons=["env.lock mode is 'best-effort'"])
        )


def test_builder_still_attests_an_eligible_exact_result(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, lock_mode="deterministic")
    attestation = build_reperformance_attestation(capsule, _result(eligible=True))
    assert attestation.match == "exact"


def test_cli_signs_nothing_for_an_ineligible_exact_replay(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, lock_mode="best-effort")
    key, _ = generate_keypair(tmp_path / "keys")
    out = tmp_path / "att.intoto.json"
    result = runner.invoke(
        evidence_app,
        [
            "attest-replay", str(capsule), "--mode", "exact", "--key", str(key),
            "--output", str(out), "--output-dir", str(tmp_path / "replays"),
        ],
    )
    assert result.exit_code != 0, result.output
    assert "not eligible" in result.output
    assert not out.exists(), "an attestation was written for an ineligible exact replay"


def test_cli_still_signs_an_eligible_exact_replay(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, lock_mode="deterministic")
    key, _ = generate_keypair(tmp_path / "keys")
    out = tmp_path / "att.intoto.json"
    result = runner.invoke(
        evidence_app,
        [
            "attest-replay", str(capsule), "--mode", "exact", "--key", str(key),
            "--output", str(out), "--output-dir", str(tmp_path / "replays"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
