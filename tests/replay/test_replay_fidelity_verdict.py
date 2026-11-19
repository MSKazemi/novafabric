"""ADR-0321: the fidelity verdict's rules, over synthetic capsules and event logs.

``faithful`` only when every computable check is equal, nothing diverged and no
call went live or past the interceptors; never by default.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from _mocked_replay_agent import mcp_record, openai_record

from novafabric.replay._contract import summarize
from novafabric.replay._fidelity import (
    REQUIRED_CHECKS,
    VERDICTS,
    compute_fidelity,
    not_run,
)


def _capsule(tmp_path: Path, *, stdout: bytes = b"hi\n", exit_code: Any = 0,
             digests: bool = True, capture_mode: str = "cli-wrapper") -> Path:
    capsule = tmp_path / "capsule"
    (capsule / "outputs").mkdir(parents=True)
    manifest: dict[str, Any] = {"run_id": "R1", "capture_mode": capture_mode}
    if exit_code is not None:
        manifest["exit_code"] = exit_code
    if stdout:
        (capsule / "outputs" / "stdout.txt").write_bytes(stdout)
    if digests:
        manifest["evidence_digests"] = {} if not stdout else {
            "outputs/stdout.txt": {"sha256": "sha256:" + hashlib.sha256(stdout).hexdigest(),
                                   "size_bytes": len(stdout)}
        }
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    return capsule


def _verdict(
    capsule: Path,
    *,
    model_calls: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    stdout: bytes | None = b"hi\n",
    stderr: bytes | None = b"",
    exit_code: int = 0,
    run_error: dict[str, Any] | None = None,
    extra_contract: dict[str, Any] | None = None,
) -> Any:
    model_calls = model_calls or []
    tool_calls = tool_calls or []
    events = [{"event": "installed"}, *(events or [])]
    report = summarize(model_calls, tool_calls, events, divergence_policy="fail",
                       substitute_tools=True)
    if extra_contract:
        original = report.as_dict

        def as_dict() -> dict[str, Any]:
            return {**original(), **extra_contract}

        report.as_dict = as_dict  # type: ignore[method-assign]
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    return compute_fidelity(
        capsule_dir=capsule, manifest=manifest, model_calls=model_calls,
        tool_calls=tool_calls, events=events, report=report, exit_code=exit_code,
        run_error=run_error, stdout=stdout, stderr=stderr, replay_capsule=None,
    )


def test_matching_outcome_and_calls_are_faithful(tmp_path: Path) -> None:
    rec = openai_record("Paris", call_id="m1")
    fidelity = _verdict(
        _capsule(tmp_path), model_calls=[rec],
        events=[{"event": "model_served", "queue": "openai", "call_index": 0,
                 "model_call_id": "m1"}],
    )
    assert fidelity.verdict == "faithful", fidelity.as_dict()
    assert fidelity.as_dict()["normalization"] == "none"
    assert fidelity.verdict in VERDICTS


@pytest.mark.parametrize("counter", [
    "model_calls_live", "network_connections_live",
    # Added by the replay-accounting work; honoured whenever the contract has them.
    "model_calls_not_servable", "child_processes_spawned",
])
def test_any_live_or_unintercepted_call_makes_faithful_impossible(
    tmp_path: Path, counter: str
) -> None:
    fidelity = _verdict(_capsule(tmp_path), extra_contract={counter: 1})
    assert fidelity.verdict == "diverged"
    assert fidelity.failing_checks == ["live_calls"]


def test_a_live_model_call_event_is_never_faithful(tmp_path: Path) -> None:
    """Under --permissive an unsupported surface runs live and diverges nothing."""
    events = [{"event": "model_live", "provider": "openai", "surface": "x.create"}]
    model_calls: list[dict[str, Any]] = []
    report = summarize(model_calls, [], [{"event": "installed"}, *events],
                       divergence_policy="warn", substitute_tools=True)
    assert not report.diverged
    manifest = yaml.safe_load((_capsule(tmp_path) / "capsule.yaml").read_text())
    fidelity = compute_fidelity(
        capsule_dir=tmp_path / "capsule", manifest=manifest, model_calls=model_calls,
        tool_calls=[], events=events, report=report, exit_code=0, run_error=None,
        stdout=b"hi\n", stderr=b"", replay_capsule=None,
    )
    assert fidelity.verdict == "diverged"
    assert set(fidelity.failing_checks) == {"live_calls", "model_call_sequence"}


def test_a_tool_on_a_surface_replay_does_not_intercept_is_never_faithful(
    tmp_path: Path,
) -> None:
    unintercepted = {"tool_call_id": "t1", "tool_name": "shell", "transport": "subprocess"}
    fidelity = _verdict(_capsule(tmp_path), tool_calls=[unintercepted])
    assert fidelity.verdict == "diverged"
    assert fidelity.failing_checks == ["live_calls"]


def test_a_divergence_is_diverged(tmp_path: Path) -> None:
    rec = openai_record("Paris", call_id="m1")
    fidelity = _verdict(_capsule(tmp_path), model_calls=[rec])  # never requested
    assert fidelity.verdict == "diverged"
    assert "divergence" in fidelity.failing_checks
    assert "model_call_sequence" in fidelity.failing_checks


def test_tool_calls_served_out_of_recorded_order_are_diverged(tmp_path: Path) -> None:
    a = mcp_record("get", {"k": 1}, "one", tool_call_id="t1")
    b = mcp_record("get", {"k": 2}, "two", tool_call_id="t2")
    events = [
        {"event": "tool_mocked", "tool_name": "get", "record_id": "t2"},
        {"event": "tool_mocked", "tool_name": "get", "record_id": "t1"},
    ]
    fidelity = _verdict(_capsule(tmp_path), tool_calls=[a, b], events=events)
    assert fidelity.failing_checks == ["tool_call_sequence"]
    in_order = _verdict(_capsule(tmp_path / "x"), tool_calls=[a, b], events=events[::-1])
    assert in_order.verdict == "faithful", in_order.as_dict()


def test_stderr_and_exit_code_are_compared(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    stderr_only = _verdict(capsule, stderr=b"warning\n")
    # stderr is advisory (owner decision 2026-11-19): reported, never in the verdict.
    assert stderr_only.failing_checks == []
    assert stderr_only.advisory_differences == ["stderr_sha256"]
    assert stderr_only.verdict == "faithful"
    check = next(c for c in stderr_only.as_dict()["checks"] if c["name"] == "stderr_sha256")
    assert check["equal"] is False and check["advisory"] is True
    assert _verdict(capsule, exit_code=2).failing_checks == ["exit_code"]
    assert _verdict(capsule, stdout=b"other\n").failing_checks == ["stdout_sha256"]


@pytest.mark.parametrize("error_type", ["ReplayTimeout", "ReplayLaunchError"])
def test_a_replay_that_did_not_complete_is_failed(tmp_path: Path, error_type: str) -> None:
    fidelity = _verdict(
        _capsule(tmp_path), stdout=None, stderr=None, exit_code=124,
        run_error={"type": error_type, "message": "stopped"},
    )
    assert fidelity.verdict == "failed"
    assert fidelity.reason == "stopped"


def test_a_dispatcher_that_was_not_installed_is_failed(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path)
    report = summarize([], [], [{"event": "install_failed", "error": "ImportError: x"}],
                       divergence_policy="fail", substitute_tools=True)
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    fidelity = compute_fidelity(
        capsule_dir=capsule, manifest=manifest, model_calls=[], tool_calls=[],
        events=[], report=report, exit_code=3, run_error=None, stdout=b"", stderr=b"",
        replay_capsule=None,
    )
    assert fidelity.verdict == "failed"


def test_a_refused_replay_is_failed_with_no_checks() -> None:
    fidelity = not_run("refused")
    assert fidelity.as_dict() == {
        "verdict": "failed", "normalization": "none", "checks": [], "reason": "refused",
    }


@pytest.mark.parametrize("kwargs", [
    {"exit_code": None},
    {"capture_mode": "sdk-decorator"},
    {"stdout": b"", "digests": False},
])
def test_missing_recorded_data_is_not_compared_never_faithful(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    capsule = _capsule(tmp_path, **kwargs)
    fidelity = _verdict(capsule, stdout=b"" if "stdout" in kwargs else b"hi\n")
    assert fidelity.verdict == "not_compared", fidelity.as_dict()
    assert fidelity.uncompared_checks
    assert set(fidelity.uncompared_checks) <= set(REQUIRED_CHECKS)


def test_an_empty_stream_absent_from_a_digest_bound_capsule_is_empty(tmp_path: Path) -> None:
    """``nova capture`` writes a stream only when not empty; ADR-0251 digests
    bind every file, so there an absent stream was empty."""
    capsule = _capsule(tmp_path, stdout=b"")
    assert _verdict(capsule, stdout=b"").verdict == "faithful"
    assert _verdict(capsule, stdout=b"x").failing_checks == ["stdout_sha256"]


def test_a_stream_that_no_longer_matches_its_evidence_digest_is_not_compared(
    tmp_path: Path,
) -> None:
    capsule = _capsule(tmp_path)
    (capsule / "outputs" / "stdout.txt").write_bytes(b"edited\n")
    fidelity = _verdict(capsule, stdout=b"edited\n")
    assert fidelity.verdict == "not_compared"
    assert fidelity.uncompared_checks == ["stdout_sha256"]


def test_a_divergence_outranks_missing_recorded_data(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, exit_code=None)
    assert _verdict(capsule, stdout=b"boom").verdict == "diverged"
