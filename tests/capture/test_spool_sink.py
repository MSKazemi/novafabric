"""Tests for the capture→spool write integration (ADR-0092 slice C / C0).

The ``SpoolSink`` converts a captured event into a schema-valid EventEnvelope v1
record and writes it to a NovaFabric spool (the Go-backed durable queue the
resident drain forwards to the collector tier). Pure-Python spool fallback is
used here, so these run on the laptop without ``libnovaspool.so``.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from novafabric.capture._ulid import new_ulid
from novafabric.capture.spool_sink import SpoolSink

_HEADER_SIZE = 16  # 16-byte binary spool segment header (matches Go segment.go)
_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent
    / "schemas"
    / "event-envelope-v1"
    / "envelope-v1.json"
)


def _read_envelopes(spool_dir: Path) -> list[dict]:
    """Return every EventEnvelope record committed to the spool, in file order."""
    envelopes: list[dict] = []
    for seg in sorted(spool_dir.glob("*.jsonl")):
        body = seg.read_bytes()[_HEADER_SIZE:]
        for line in body.splitlines():
            if line.strip():
                envelopes.append(json.loads(line))
    return envelopes


def test_emit_event_writes_schema_valid_envelope_to_spool(tmp_path: Path) -> None:
    run_id = new_ulid()
    sink = SpoolSink(tmp_path / "spool")
    sink.emit_event(event_type="model_call", run_id=run_id, agent_id="agent-1")
    sink.close()

    envelopes = _read_envelopes(tmp_path / "spool")
    assert len(envelopes) == 1

    schema = json.loads(_SCHEMA_PATH.read_text())
    jsonschema.validate(envelopes[0], schema)  # raises on violation
    assert envelopes[0]["event_type"] == "model_call"
    assert envelopes[0]["run_id"] == run_id


def test_emit_event_preserves_payload_and_defaults_global_run_id(tmp_path: Path) -> None:
    run_id = new_ulid()
    sink = SpoolSink(tmp_path / "spool")
    sink.emit_event(
        event_type="tool_call",
        run_id=run_id,
        agent_id="agent-1",
        payload={"tool": "search", "args": {"q": "x"}},
    )
    sink.close()

    [env] = _read_envelopes(tmp_path / "spool")
    assert env["payload"] == {"tool": "search", "args": {"q": "x"}}
    # global_run_id defaults to run_id for STANDALONE/PARENT capsules.
    assert env["global_run_id"] == run_id
    assert env["parent_run_id"] is None


def test_multiple_emits_write_one_record_each(tmp_path: Path) -> None:
    run_id = new_ulid()
    sink = SpoolSink(tmp_path / "spool")
    for _ in range(5):
        sink.emit_event(event_type="span", run_id=run_id, agent_id="agent-1")
    sink.close()

    assert len(_read_envelopes(tmp_path / "spool")) == 5


def test_emit_event_is_fail_open_on_write_error(tmp_path: Path) -> None:
    """A spool write failure must never surface to the agent workflow."""
    sink = SpoolSink(tmp_path / "spool")

    def _boom(_: object) -> None:
        raise OSError("spool full")

    sink._spool.write = _boom  # type: ignore[method-assign]
    # Must not raise.
    sink.emit_event(event_type="run.start", run_id=new_ulid(), agent_id="agent-1")
    sink.close()
