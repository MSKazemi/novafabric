"""An exact-mode re-performance attestation must not be signed for an ineligible run.

``--mode exact`` re-executes nothing: ``ReplayEngine._exact`` checks whether the
recorded run meets the exact-replay preconditions and reports the answer in
``exact_eligible``, with ``status="success"`` either way. ``verdict_for`` maps a
successful exact-mode result to ``match: exact``, so before this guard
``nova evidence attest-replay --mode exact`` signed ``match: exact`` for a run whose
own result said ``exact_eligible: false``. Session replay already refuses that case
(``session/replay.py``); the attestation path now refuses it too — nothing is signed
(ADR-0269 option B, "refuse to attest what was not performed").

The builder is pinned by an exhaustive truth table over every mode × eligibility ×
status (48 cases, in memory) against an oracle written here, independently of
``verdict_for``: the guard must refuse exactly the ineligible exact cases and leave
every other mode's verdict untouched. Two CLI runs pin the operator path end to end,
including that the signed envelope says what the builder decided.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from novafabric.cli.evidence import AttestReplayMode, evidence_app
from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.reperformance import (
    NotAttestableError,
    build_reperformance_attestation,
)
from novafabric.evidence.signing import generate_keypair, verify_with_pem
from novafabric.replay._result import ReplayResult

runner = CliRunner()
MODES = [m.value for m in AttestReplayMode]
STATUSES = ["success", "failure", "aborted", "dry_run"]
ELIGIBILITY = [True, False, None]


def _result(
    *,
    mode: str = "exact",
    status: str = "success",
    eligible: bool | None,
    reasons: list[str] | None = None,
) -> ReplayResult:
    return ReplayResult(
        replay_id="rp-x",
        replay_of_run_id="run-x",
        mode=mode,
        status=status,
        start_time="2026-10-22T00:00:00Z",
        end_time="2026-10-22T00:00:01Z",
        duration_ms=1000,
        policy_flags_used=[f"--mode={mode}"],
        env_warnings=[],
        exact_eligible=eligible,
        exact_reasons=reasons,
    )


def _capsule(root: Path, *, lock_mode: str) -> Path:
    capsule = root / "run-exact"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        yaml.dump({"run_id": "run-exact", "command": ["python", "agent.py"]})
    )
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"gen_ai.request.model": "m", "gen_ai.request.seed": 7}) + "\n"
    )
    (capsule / "env.lock").write_text(yaml.dump({"mode": lock_mode}))
    return capsule


@pytest.fixture(scope="module")
def capsule(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _capsule(tmp_path_factory.mktemp("capsule"), lock_mode="deterministic")


def _expected(mode: str, status: str, eligible: bool | None) -> str | None:
    """The oracle: ``None`` means "must refuse"; otherwise the verdict to sign."""
    if mode == "exact" and eligible is not True:
        return None
    if status != "success":
        return "mismatch"
    return "exact" if mode == "exact" else "semantic-match"


def test_the_cli_offers_the_modes_the_truth_table_covers() -> None:
    assert set(MODES) == {"mocked", "forensic", "semantic", "exact"}


@pytest.mark.parametrize(
    ("mode", "status", "eligible"),
    list(itertools.product(MODES, STATUSES, ELIGIBILITY)),
    ids=str,
)
def test_the_guard_refuses_exactly_the_ineligible_exact_results(
    capsule: Path, mode: str, status: str, eligible: bool | None
) -> None:
    result = _result(mode=mode, status=status, eligible=eligible)
    expected = _expected(mode, status, eligible)
    if expected is None:
        with pytest.raises(NotAttestableError, match="not eligible"):
            build_reperformance_attestation(capsule, result)
    else:
        attestation = build_reperformance_attestation(capsule, result)
        assert attestation.match == expected
        assert attestation.replay_mode == mode


def test_the_refusal_names_the_run_and_every_recorded_reason(capsule: Path) -> None:
    reasons = ["env.lock mode is 'best-effort'", "no seed on call 2"]
    with pytest.raises(NotAttestableError) as caught:
        build_reperformance_attestation(capsule, _result(eligible=False, reasons=reasons))
    assert "run-x" in str(caught.value)
    assert all(r in str(caught.value) for r in reasons)


@pytest.mark.parametrize("reasons", [None, []])
def test_an_unassessed_result_says_eligibility_was_not_established(
    capsule: Path, reasons: list[str] | None
) -> None:
    with pytest.raises(NotAttestableError, match="eligibility was not established"):
        build_reperformance_attestation(capsule, _result(eligible=None, reasons=reasons))


def test_the_refusal_is_a_value_error_for_existing_callers() -> None:
    assert issubclass(NotAttestableError, ValueError)


# ── nova evidence attest-replay end to end ─────────────────────────────────────


def _attest(tmp_path: Path, capsule_dir: Path, key: Path, out: Path) -> Result:
    return runner.invoke(
        evidence_app,
        [
            "attest-replay", str(capsule_dir), "--mode", "exact", "--key", str(key),
            "--output", str(out), "--output-dir", str(tmp_path / "replays"),
        ],
    )


def test_cli_signs_nothing_for_an_ineligible_exact_replay(tmp_path: Path) -> None:
    capsule_dir = _capsule(tmp_path, lock_mode="best-effort")
    key, _ = generate_keypair(tmp_path / "keys")
    out = tmp_path / "att.intoto.json"
    result = _attest(tmp_path, capsule_dir, key, out)
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)  # not a traceback
    said = " ".join(result.output.split())  # Rich wraps at the terminal width
    assert "not eligible" in said
    assert "nothing was signed" in said
    assert not out.exists(), "an attestation was written for an ineligible exact replay"
    assert not list(tmp_path.glob("**/*.intoto.json"))


def test_cli_signs_an_eligible_exact_replay_and_the_envelope_says_exact(tmp_path: Path) -> None:
    capsule_dir = _capsule(tmp_path, lock_mode="deterministic")
    key, pub = generate_keypair(tmp_path / "keys")
    out = tmp_path / "att.intoto.json"
    result = _attest(tmp_path, capsule_dir, key, out)
    assert result.exit_code == 0, result.output
    pem = pub.read_bytes()
    statement = dsse_verify(
        json.loads(out.read_text()), lambda pae, sig: verify_with_pem(pem, pae, sig)
    )
    assert statement["predicate"]["match"] == "exact"
    assert statement["predicate"]["replay_mode"] == "exact"
    capsule_sha = hashlib.sha256((capsule_dir / "capsule.yaml").read_bytes()).hexdigest()
    assert statement["subject"][0]["digest"]["sha256"] == capsule_sha
