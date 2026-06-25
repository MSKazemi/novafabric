"""Offset-replay rebuild tests (gap-002 / SI-1 productization, ADR-0020)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.collector import collector_app
from novafabric.evidence_fabric.rebuild import (
    OffsetReplayRebuilder,
    RebuildReport,
    rebuild_from_events,
)


def _event(run_id: str, seq: int) -> bytes:
    return json.dumps({"run_id": run_id, "seq": seq, "payload": f"p{seq}"}).encode()


class TestRebuildCore:
    def test_routes_per_run_with_digests(self, tmp_path: Path) -> None:
        events = [_event("run-a", 0), _event("run-b", 0), _event("run-a", 1)]
        report = rebuild_from_events(events, tmp_path)
        assert report.events_replayed == 3
        assert {r.run_id for r in report.runs} == {"run-a", "run-b"}
        assert report.order_preserved is True
        a_lines = (tmp_path / "run-a.jsonl").read_text().splitlines()
        assert len(a_lines) == 2
        assert json.loads(a_lines[1])["seq"] == 1

    def test_rebuild_is_byte_equal_deterministic(self, tmp_path: Path) -> None:
        events = [_event("run-a", i) for i in range(50)]
        first = rebuild_from_events(events, tmp_path / "one")
        second = rebuild_from_events(events, tmp_path / "two")
        assert first.digest_map == second.digest_map

    def test_out_of_order_seq_flagged_not_dropped(self, tmp_path: Path) -> None:
        events = [_event("run-a", 1), _event("run-a", 0)]
        report = rebuild_from_events(events, tmp_path)
        assert report.order_preserved is False
        assert report.runs[0].events == 2  # never dropped

    def test_unattributed_events_preserved(self, tmp_path: Path) -> None:
        events = [b"not json at all", json.dumps({"no_run": True}).encode()]
        report = rebuild_from_events(events, tmp_path)
        assert report.runs[0].run_id == "_unattributed"
        assert report.runs[0].events == 2
        assert (tmp_path / "_unattributed.jsonl").exists()

    def test_empty_buffer_yields_empty_report(self, tmp_path: Path) -> None:
        report = rebuild_from_events([], tmp_path)
        assert report.events_replayed == 0
        assert report.runs == []
        assert report.order_preserved is True

    def test_report_round_trips_json(self, tmp_path: Path) -> None:
        report = rebuild_from_events([_event("run-a", 0)], tmp_path)
        again = RebuildReport.model_validate_json(report.model_dump_json())
        assert again == report


class TestCli:
    def test_rebuild_cli_with_stubbed_drain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_drain(self: OffsetReplayRebuilder) -> list[bytes]:
            return [_event("run-a", 0), _event("run-a", 1)]

        monkeypatch.setattr(OffsetReplayRebuilder, "drain", fake_drain)
        out = tmp_path / "rebuilt"
        report_file = tmp_path / "report.json"
        runner = CliRunner()
        result = runner.invoke(
            collector_app,
            ["rebuild", "--target", str(out), "--report", str(report_file)],
        )
        assert result.exit_code == 0, result.output
        assert "Replayed 2 event(s)" in result.output
        payload = json.loads(report_file.read_text())
        assert payload["order_preserved"] is True

    def test_rebuild_cli_exit_2_on_order_violation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_drain(self: OffsetReplayRebuilder) -> list[bytes]:
            return [_event("run-a", 5), _event("run-a", 1)]

        monkeypatch.setattr(OffsetReplayRebuilder, "drain", fake_drain)
        runner = CliRunner()
        result = runner.invoke(
            collector_app, ["rebuild", "--target", str(tmp_path / "r")]
        )
        assert result.exit_code == 2
        assert "ORDER VIOLATION" in result.output

    def test_rebuild_cli_broker_unreachable(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            collector_app,
            [
                "rebuild",
                "--target",
                str(tmp_path / "r"),
                "--nats-url",
                "nats://127.0.0.1:1",  # nothing listens here
            ],
        )
        assert result.exit_code == 1


@pytest.mark.skipif(
    not os.environ.get("NOVA_NATS_URL"),
    reason="integration: requires a live NATS JetStream (set NOVA_NATS_URL)",
)
class TestIntegration:
    def test_round_trip_against_live_jetstream(self, tmp_path: Path) -> None:
        import asyncio

        async def scenario() -> RebuildReport:
            import nats
            from nats.js.api import StorageType, StreamConfig

            nc = await nats.connect(os.environ["NOVA_NATS_URL"])
            js = nc.jetstream()
            stream = "NOVA_REBUILD_IT"
            try:
                await js.delete_stream(stream)
            except Exception:
                pass
            await js.add_stream(
                StreamConfig(
                    name=stream,
                    subjects=["nova.rebuild.it.>"],
                    storage=StorageType.FILE,
                )
            )
            for i in range(20):
                await js.publish("nova.rebuild.it.run-x", _event("run-x", i))
            await nc.close()
            rebuilder = OffsetReplayRebuilder(
                stream=stream, subject="nova.rebuild.it.>"
            )
            report = await rebuilder.rebuild(tmp_path / "out")
            nc = await nats.connect(os.environ["NOVA_NATS_URL"])
            await nc.jetstream().delete_stream(stream)
            await nc.close()
            return report

        report = asyncio.run(scenario())
        assert report.events_replayed == 20
        assert report.order_preserved is True
