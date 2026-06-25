"""C0.2 — CaptureOrchestrator → SpoolSink wiring (ADR-0092 slice C).

When spool emission is opted in, the orchestrator writes run-boundary
EventEnvelope v1 records (``run.start`` + ``capsule.finalize``) to the spool so
the resident drain can forward them to the collector tier. Off by default —
local capture is unaffected. Laptop-tested via the pure-Python spool fallback.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app

_HEADER_SIZE = 16
_SCHEMA_PATH = (
    Path(__file__).parents[2] / "schemas" / "event-envelope-v1" / "envelope-v1.json"
)


def _read_envelopes(spool_dir: Path) -> list[dict]:
    envelopes: list[dict] = []
    for seg in sorted(spool_dir.glob("*.jsonl")):
        body = seg.read_bytes()[_HEADER_SIZE:]
        for line in body.splitlines():
            if line.strip():
                envelopes.append(json.loads(line))
    return envelopes


def test_emit_spool_off_by_default_writes_no_spool(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    spool_dir = tmp_path / "spool"
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    orch.run(command=[sys.executable, str(script)])
    assert not spool_dir.exists() or _read_envelopes(spool_dir) == []


def test_emit_spool_writes_run_boundary_envelopes(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    spool_dir = tmp_path / "spool"
    orch = CaptureOrchestrator(
        base_dir=tmp_path / "runs", emit_spool=True, spool_dir=spool_dir
    )
    result = orch.run(command=[sys.executable, str(script)])

    envelopes = _read_envelopes(spool_dir)
    types = [e["event_type"] for e in envelopes]
    assert "run.start" in types
    assert "capsule.finalize" in types

    schema = json.loads(_SCHEMA_PATH.read_text())
    for env in envelopes:
        jsonschema.validate(env, schema)
        assert env["run_id"] == result.run_id


def test_cli_capture_emit_spool_writes_envelopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nova capture --emit-spool` resolves the default spool dir and emits."""
    spool = tmp_path / "spool"
    monkeypatch.setenv("NOVAFABRIC_SPOOL_DIR", str(spool))
    result = CliRunner().invoke(
        app,
        [
            "capture",
            "--emit-spool",
            "--output-dir", str(tmp_path / "runs"),
            sys.executable, "-c", "pass",
        ],
    )
    assert result.exit_code == 0, result.output
    types = {e["event_type"] for e in _read_envelopes(spool)}
    assert {"run.start", "capsule.finalize"} <= types
