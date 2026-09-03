"""ADR-0147 I-4: every drift/assure CLI output carries the assurance-honesty line.

This is not decoration. The detectors render verdict-shaped output — ``DRIFTED``
in red, ``silent-failure`` in red — and ADR-0147 exists partly because a detector
that looks like a gate gets treated as one. The line is what separates "this
exceeded the threshold you declared" from "NovaFabric judges your model broken".

The load-bearing test is `test_every_drift_command_carries_the_line`: it walks the
Typer app rather than naming three commands, so a fourth cannot ship without it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.assure._honesty import HONESTY_LINE
from novafabric.cli.drift import app as drift_app
from novafabric.cli.main import app

runner = CliRunner()

#: Enough of the line to survive rich's line wrapping in a narrow terminal.
_MARKERS = ("remediate", "retrain", "roll back")


def _carries_line(text: str) -> bool:
    flat = " ".join(text.split())
    return all(m in flat for m in _MARKERS)


@pytest.fixture()
def drift_doc(tmp_path: Path) -> Path:
    p = tmp_path / "drift.json"
    p.write_text(json.dumps({
        "kind": "output", "metric": "response-length-dist", "statistic": "psi",
        "baseline": [10, 11, 12, 10, 11], "window": [30, 31, 32, 30, 31],
        "threshold": 0.2,
    }))
    return p


@pytest.fixture()
def silent_doc(tmp_path: Path) -> Path:
    p = tmp_path / "silent.json"
    p.write_text(json.dumps({
        "threshold": 0.5,
        "runs": [{"run_id": "r1", "status": "success", "quality_signal": 0.2}],
    }))
    return p


@pytest.fixture()
def rootcause_doc(tmp_path: Path) -> Path:
    p = tmp_path / "rc.json"
    p.write_text(json.dumps({
        "baseline": [{"kind": "model", "ref": "gpt-a"}],
        "drifted": [{"kind": "model", "ref": "gpt-b"}],
    }))
    return p


@pytest.fixture()
def fingerprint_doc(tmp_path: Path) -> Path:
    p = tmp_path / "fingerprint.json"
    p.write_text(json.dumps({
        "run": {"run_id": "r1", "calls": [{"name": "search", "arguments": {"q": "a"}}]},
        "baseline": {"run_id": "b1", "calls": [{"name": "deploy", "arguments": {}}]},
        "threshold": 0.2,
    }))
    return p


@pytest.fixture()
def collect_store(tmp_path: Path) -> Path:
    """A one-capsule store, so `nova drift collect` has something to collect."""
    root = tmp_path / "capsules"
    (root / "run-1").mkdir(parents=True)
    (root / "run-1" / "capsule.json").write_text(json.dumps({
        "run_id": "run-1", "created_at": "2026-07-01T00:00:00Z", "status": "success",
    }))
    return root


# ── the guard ────────────────────────────────────────────────────────────────


def test_the_line_is_defined_once() -> None:
    """Two CLI groups print it; a second copy would let them drift apart."""
    from novafabric.cli.assure_baseline import HONESTY_LINE as baseline_line

    assert baseline_line is HONESTY_LINE


def test_every_drift_command_carries_the_line(
    drift_doc: Path,
    silent_doc: Path,
    rootcause_doc: Path,
    fingerprint_doc: Path,
    collect_store: Path,
) -> None:
    """Walks the app, so a newly added command is covered without editing this test."""
    invocations = {
        "detect": ["drift", "detect", str(drift_doc)],
        "silent-failure": ["drift", "silent-failure", str(silent_doc)],
        "root-cause": ["drift", "root-cause", str(rootcause_doc)],
        "fingerprint": ["drift", "fingerprint", str(fingerprint_doc)],
        "collect": [
            "drift", "collect", "--capsules", str(collect_store),
            "--window", "..", "--no-cache",
        ],
    }
    registered = {c.name or c.callback.__name__ for c in drift_app.registered_commands}
    assert registered == set(invocations), (
        f"nova drift commands changed: {registered ^ set(invocations)}. "
        "Add the new command here — ADR-0147 I-4 requires the honesty line on every one."
    )

    for name, argv in invocations.items():
        result = runner.invoke(app, argv)
        assert result.exit_code == 0, f"{name}: {result.output}"
        assert _carries_line(result.output), f"{name} is missing the honesty line"


def test_json_output_stays_parseable(drift_doc: Path) -> None:
    """The line is a disclosure about the output, never part of it."""
    result = runner.invoke(app, ["drift", "detect", str(drift_doc), "--json"])

    assert result.exit_code == 0
    # stdout must be JSON and nothing else; the line rides on stderr.
    stdout = result.stdout
    parsed = json.loads(stdout)
    assert parsed["drifted"] is True
    assert not _carries_line(stdout), "the honesty line must not corrupt --json stdout"


def test_assure_baseline_carries_it_too(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("run_id: r\n")

    result = runner.invoke(app, [
        "assure-baseline", "pin", "--capsule", str(capsule), "--run", "r",
        "--id", "bl", "--criterion", "goal", "--pinned-at", "2026-07-01T00:00:00Z",
    ])
    assert result.exit_code == 0, result.output
    assert _carries_line(result.output)
