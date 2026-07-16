"""``nova events tail|emit`` CLI (experimental, ADR-0137)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _emit(log: Path, **kwargs: str) -> Any:
    args = ["events", "emit"]
    for key, value in kwargs.items():
        args.extend([f"--{key.replace('_', '-')}", value])
    return runner.invoke(app, args, env={"NOVA_EVENTS_LOG": str(log)})


class TestEventsEmit:
    def test_emit_writes_log(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        result = _emit(log, type="capsule.created", subject="capsule:run-abc")
        assert result.exit_code == 0, result.output
        assert "Emitted capsule.created event" in result.output
        record = json.loads(log.read_text().splitlines()[0])
        assert record["type"] == "capsule.created"
        assert record["subject"]["ref"] == "run-abc"

    def test_emit_with_payload_and_digest(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        result = _emit(
            log,
            type="policy.failed",
            subject="policy:promotion:agent-a@1.4.0",
            digest="sha256:" + "ab" * 32,
            payload='{"decision": "deny", "gate": "eval-regression"}',
        )
        assert result.exit_code == 0, result.output
        record = json.loads(log.read_text().splitlines()[0])
        assert record["payload"]["decision"] == "deny"
        # subject REF may itself contain ":" — only the first colon splits
        assert record["subject"] == {
            "kind": "policy",
            "ref": "promotion:agent-a@1.4.0",
            "digest": "sha256:" + "ab" * 32,
        }

    def test_emit_without_sink_fails(self) -> None:
        result = runner.invoke(
            app,
            ["events", "emit", "--type", "capsule.created",
             "--subject", "capsule:run-abc"],
        )
        assert result.exit_code == 1
        assert "no sink configured" in result.output

    def test_emit_unknown_type_fails(self, tmp_path: Path) -> None:
        result = _emit(tmp_path / "e.jsonl", type="capsule.exploded",
                       subject="capsule:run-abc")
        assert result.exit_code != 0

    def test_emit_bad_subject_fails(self, tmp_path: Path) -> None:
        result = _emit(tmp_path / "e.jsonl", type="capsule.created",
                       subject="run-abc")
        assert result.exit_code != 0

    def test_emit_bad_payload_fails(self, tmp_path: Path) -> None:
        result = _emit(tmp_path / "e.jsonl", type="capsule.created",
                       subject="capsule:run-abc", payload="[1,2]")
        assert result.exit_code != 0


class TestEventsTail:
    @pytest.fixture
    def log(self, tmp_path: Path) -> Path:
        path = tmp_path / "events.jsonl"
        for result in [
            _emit(path, type="capsule.created", subject="capsule:run-1"),
            _emit(path, type="capsule.validated", subject="capsule:run-1"),
            _emit(path, type="capsule.created", subject="capsule:run-2"),
        ]:
            assert result.exit_code == 0
        return path

    def test_tail_prints_all(self, log: Path) -> None:
        result = runner.invoke(app, ["events", "tail", "--log", str(log)])
        assert result.exit_code == 0, result.output
        assert result.output.count("capsule.created") == 2
        assert result.output.count("capsule.validated") == 1

    def test_tail_type_filter(self, log: Path) -> None:
        result = runner.invoke(
            app,
            ["events", "tail", "--log", str(log), "--type", "capsule.validated"],
        )
        assert "capsule.created" not in result.output
        assert "capsule.validated" in result.output

    def test_tail_json_output(self, log: Path) -> None:
        result = runner.invoke(
            app, ["events", "tail", "--log", str(log), "--json", "--last", "1"]
        )
        record = json.loads(result.output.strip())
        assert record["subject"]["ref"] == "run-2"

    def test_tail_since_filter(self, log: Path) -> None:
        result = runner.invoke(
            app,
            ["events", "tail", "--log", str(log), "--since", "2020-01-01T00:00:00Z"],
        )
        assert result.output.count("capsule") >= 3
        result = runner.invoke(
            app,
            ["events", "tail", "--log", str(log), "--since", "2999-01-01T00:00:00Z"],
        )
        assert "No matching events." in result.output

    def test_tail_env_log_path(self, log: Path) -> None:
        result = runner.invoke(
            app, ["events", "tail"], env={"NOVA_EVENTS_LOG": str(log)}
        )
        assert result.exit_code == 0

    def test_tail_no_log_configured_fails(self) -> None:
        result = runner.invoke(app, ["events", "tail"])
        assert result.exit_code == 1
        assert "no events log configured" in result.output

    def test_tail_missing_file_fails(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app, ["events", "tail", "--log", str(tmp_path / "nope.jsonl")]
        )
        assert result.exit_code == 1

    def test_tail_skips_malformed_lines(self, log: Path) -> None:
        with log.open("a") as fh:
            fh.write("{not json\n")
        result = runner.invoke(app, ["events", "tail", "--log", str(log)])
        assert result.exit_code == 0
        assert "skipping malformed line" in result.output

    def test_tail_bad_since_fails(self, log: Path) -> None:
        result = runner.invoke(
            app, ["events", "tail", "--log", str(log), "--since", "yesterday"]
        )
        assert result.exit_code != 0


class TestHelp:
    def test_events_help(self) -> None:
        result = runner.invoke(app, ["events", "--help"])
        assert result.exit_code == 0
        assert "tail" in result.output
        assert "emit" in result.output
