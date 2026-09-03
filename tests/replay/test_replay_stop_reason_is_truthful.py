"""A replay result must not assert an exit that never happened.

`_run_mocked_subprocess` returns `124` on timeout — the conventional shell code —
and the failure branch then recorded
`{"type": "NonZeroExit", "message": "Replayed command exited with code 124"}`.
That is a **false statement**: the command did not exit, it was killed. The same
branch described a command that never launched as having "exited with code 1".

A command may also legitimately exit `124` itself, so the exit code alone cannot
distinguish the two — which is why the reason travels separately.

Scope note: this fixes what the record *says*. It deliberately does **not** change
`status` (still `failure`) or `verdict_for()` (still `mismatch`), because those
change what a *signed* `ReperformanceAttestation` asserts. See
`test_the_signed_verdict_still_conflates_them` below, which documents the
remaining conflation rather than pretending it is fixed.
"""

from __future__ import annotations

import subprocess

import pytest

from novafabric.evidence.reperformance import verdict_for
from novafabric.replay._engine import REPLAY_SUBPROCESS_TIMEOUT_S, ReplayEngine
from novafabric.replay._result import ReplayResult


def _engine() -> ReplayEngine:
    return ReplayEngine.__new__(ReplayEngine)


def test_a_timeout_is_reported_as_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="sleep", timeout=REPLAY_SUBPROCESS_TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", boom)
    code, error = _engine()._run_mocked_subprocess(["sleep", "999"], [])

    assert code == 124
    assert error is not None
    assert error["type"] == "ReplayTimeout"
    assert "did not exit on its own" in error["message"]
    assert "exited with code" not in error["message"], (
        "the command did not exit; saying so would be a false statement"
    )


def test_a_launch_failure_is_not_described_as_an_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no such file"))
    )
    code, error = _engine()._run_mocked_subprocess(["nope"], [])

    assert code == 1
    assert error is not None
    assert error["type"] == "ReplayLaunchError"
    assert "could not be run" in error["message"]


def test_an_ordinary_non_zero_exit_carries_no_special_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exit code says it all here, so nothing overrides NonZeroExit."""

    class _Proc:
        returncode = 3

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    code, error = _engine()._run_mocked_subprocess(["false"], [])

    assert code == 3
    assert error is None


def test_a_command_that_genuinely_exits_124_is_not_called_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """124 is a real exit code a program may return — the reason disambiguates."""

    class _Proc:
        returncode = 124

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    code, error = _engine()._run_mocked_subprocess(["exit124"], [])

    assert code == 124
    assert error is None, "an exit code alone must not be read as a timeout"


def test_the_timeout_is_a_named_constant() -> None:
    """It is reported in the message, so it must not be a bare literal."""
    assert isinstance(REPLAY_SUBPROCESS_TIMEOUT_S, int)
    assert REPLAY_SUBPROCESS_TIMEOUT_S > 0


# ── the part that is NOT fixed, documented so it cannot change silently ──────


def test_the_signed_verdict_still_conflates_them() -> None:
    """A timed-out replay is still signed as `mismatch` — i.e. as having disagreed.

    `verdict_for()` maps any non-success to `mismatch`, so a replay that never
    finished produces a signed `ReperformanceAttestation` asserting the outcome
    did not match. `schemas/replay-result.schema.json` declares an `interrupted`
    status for exactly this state and the engine never emits it.

    This is recorded, not fixed: changing what a signed attestation means is an
    ADR-level decision about an existing evidence artifact. This test exists so
    the behaviour cannot change silently in either direction.
    """
    timed_out = ReplayResult(
        replay_id="r", replay_of_run_id="run", mode="mocked", status="failure",
        start_time="t", end_time="t", duration_ms=1, policy_flags_used=[],
        env_warnings=[], exit_code=124,
        error={"type": "ReplayTimeout", "message": "…"},
    )

    assert verdict_for(timed_out) == "mismatch", (
        "if this changed, the ADR decision was taken — update this test and the "
        "note in docs, do not just re-baseline it"
    )
