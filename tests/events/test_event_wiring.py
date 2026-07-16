"""Lifecycle transitions actually emit (ADR-0137 P3 slice: capsule.created/validated).

End-to-end: a real `nova capture` fires a webhook and appends to the local
log; `nova validate` emits capsule.validated; an unconfigured run emits
nothing anywhere (opt-in hard invariant).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app

SCHEMA_PATH = (
    Path(__file__).parents[2] / "src/novafabric/schemas/lifecycle-event.schema.json"
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))


def _capture(tmp_path: Path) -> Any:
    script = tmp_path / "agent.py"
    script.write_text("print('hello')\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    return orch.run(command=[sys.executable, str(script)])


def _read_events(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


class TestCaptureEmitsCapsuleCreated:
    def test_local_log_and_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        result = _capture(tmp_path)
        assert result.exit_code == 0

        events = _read_events(log)
        assert len(events) == 1
        event = events[0]
        assert event["type"] == "capsule.created"
        assert event["subject"]["kind"] == "capsule"
        assert event["subject"]["ref"] == result.run_id
        assert event["subject"]["digest"].startswith("sha256:")
        schema = json.loads(SCHEMA_PATH.read_text())
        jsonschema.validate(event, schema, format_checker=jsonschema.FormatChecker())

    def test_webhook_fires_on_capture(
        self, tmp_path: Path, webhook_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_EVENTS_WEBHOOK", webhook_server.url)
        result = _capture(tmp_path)
        assert result.exit_code == 0
        assert len(webhook_server.received) == 1
        event = json.loads(webhook_server.received[0]["body"])
        assert event["type"] == "capsule.created"
        assert event["subject"]["ref"] == result.run_id

    def test_payload_has_no_capsule_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refs, digests, enums, and counts only — never workload output."""
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        _capture(tmp_path)
        event = _read_events(log)[0]
        assert set(event["payload"]) == {
            "status", "exit_code", "duration_ms",
            "model_call_count", "tool_call_count",
        }
        for value in event["payload"].values():
            assert isinstance(value, (int, str))
        # the workload printed "hello" — it must not leak into the event
        assert "hello" not in json.dumps(event)

    def test_unreachable_webhook_never_blocks_capture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_EVENTS_WEBHOOK", "http://127.0.0.1:9/nope")
        monkeypatch.setenv("NOVA_EVENTS_MAX_RETRIES", "1")
        monkeypatch.setenv("NOVA_EVENTS_TIMEOUT_S", "0.2")
        result = _capture(tmp_path)
        assert result.exit_code == 0  # capture succeeded despite dead webhook

    def test_unconfigured_capture_emits_nothing(self, tmp_path: Path) -> None:
        result = _capture(tmp_path)
        assert result.exit_code == 0
        assert not list(tmp_path.rglob("events.jsonl"))


class TestValidateEmitsCapsuleValidated:
    def test_validate_pass_emits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture(tmp_path)
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        result = runner.invoke(app, ["validate", str(captured.capsule_dir)])
        assert result.exit_code == 0, result.output
        events = [e for e in _read_events(log) if e["type"] == "capsule.validated"]
        assert len(events) == 1
        assert events[0]["subject"]["ref"] == captured.run_id
        assert events[0]["payload"] == {"result": "pass", "error_count": 0}

    def test_validate_fail_emits_and_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _capture(tmp_path)
        (captured.capsule_dir / "trace.jsonl").unlink()
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        result = runner.invoke(app, ["validate", str(captured.capsule_dir)])
        assert result.exit_code == 1
        events = [e for e in _read_events(log) if e["type"] == "capsule.validated"]
        assert events[0]["payload"]["result"] == "fail"
        assert events[0]["payload"]["error_count"] >= 1
