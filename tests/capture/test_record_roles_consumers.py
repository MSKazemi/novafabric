"""ADR-0305: every consumer that counts or iterates model calls sees logical calls.

Two capsules with the same six calls: the pre-ADR-0305 fixture (markers stripped
from a real capture; 11 records) and the same records carrying the markers. Each
consumer must report 6, never 11, on both -- the marked one by the marker, the
old one by the reader-side fallback.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pre-adr0305-double-record"
EXPECTED = json.loads((FIXTURE / "expected.json").read_text())
LOGICAL = EXPECTED["logical"]
RECORDS = EXPECTED["records"]


def _marked(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-attach the ADR-0305 markers the fixture had stripped."""
    from novafabric.capture.record_roles import classify_model_calls

    out = []
    for r, role in zip(records, classify_model_calls(records), strict=True):
        r = json.loads(json.dumps(r))
        ext = r.setdefault("extensions", {})
        ext["io.novafabric.record_role"] = role.role
        ext["io.novafabric.logical_call_id"] = role.logical_call_id
        out.append(r)
    return out


@pytest.fixture(params=["pre-adr0305", "marked"])
def capsule(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    cap = tmp_path / "01JROLES000000000000000000"
    cap.mkdir()
    records = [
        json.loads(x) for x in (FIXTURE / "model-calls.jsonl").read_text().splitlines()
    ]
    if request.param == "marked":
        records = _marked(records)
    (cap / "model-calls.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records)
    )
    for name in ("tool-calls.jsonl", "trace.jsonl", "assets.jsonl"):
        (cap / name).write_text("")
    (cap / "capsule.yaml").write_text(yaml.safe_dump({
        "schema_version": "0.1.0", "run_id": cap.name,
        "created_at": "2026-10-09T00:00:00Z", "finished_at": "2026-10-09T00:00:01Z",
        "status": "success", "model_call_count": RECORDS, "tool_call_count": 0,
    }))
    return cap


def test_fixture_is_the_doubled_shape(capsule: Path) -> None:
    lines = (capsule / "model-calls.jsonl").read_text().splitlines()
    assert len(lines) == RECORDS == 11 and len(LOGICAL) == 6


def test_count_used_for_the_manifest(capsule: Path) -> None:
    from novafabric.capture.record_roles import count_logical_model_calls_in_file

    assert count_logical_model_calls_in_file(capsule / "model-calls.jsonl") == 6


def test_cost_estimate_counts_logical_calls(capsule: Path) -> None:
    from novafabric.cli.main import app

    result = CliRunner().invoke(app, ["cost", "estimate", str(capsule), "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["calls"] == 6


def test_query_index_has_one_row_per_logical_call(capsule: Path) -> None:
    from novafabric.query.indexer import scan_capsule

    scanned = scan_capsule(capsule)
    assert scanned is not None
    calls, _ = scanned
    assert len(calls) == 6


def test_dashboard_run_detail_splits_transport(capsule: Path) -> None:
    from novafabric.serve.capsule_loader import load_full_capsule

    full = load_full_capsule(capsule)
    assert [r["model_call_id"] for r in full["model_calls"]] == LOGICAL
    assert len(full["transport_model_calls"]) == RECORDS - 6


def test_replay_engine_loads_logical_calls(capsule: Path) -> None:
    from novafabric.capture.record_roles import read_logical_model_calls
    from novafabric.replay._contract import is_replayable_model_call

    calls = read_logical_model_calls(capsule / "model-calls.jsonl")
    assert [r["model_call_id"] for r in calls] == LOGICAL
    # Served records are a subset of logical ones; transport never is.
    all_records = [
        json.loads(x) for x in (capsule / "model-calls.jsonl").read_text().splitlines()
    ]
    for r in all_records:
        role = (r.get("extensions") or {}).get("io.novafabric.record_role")
        if role == "transport":
            assert not is_replayable_model_call(r)


def test_agent_graph_has_one_node_per_logical_call(capsule: Path) -> None:
    from novafabric.agent_graph.builder import build_agent_graph

    graph = build_agent_graph(capsule)
    ids = {n.id for n in graph.nodes}
    assert set(LOGICAL) <= ids
    assert not (ids & ({
        json.loads(x)["model_call_id"]
        for x in (capsule / "model-calls.jsonl").read_text().splitlines()
    } - set(LOGICAL)))


def test_spool_call_events(capsule: Path) -> None:
    from novafabric.capture.spool_sink import emit_call_events_from_capsule

    class Sink:
        def __init__(self) -> None:
            self.events: list[str] = []

        def emit_event(self, *, event_type: str, **_: Any) -> None:
            self.events.append(event_type)

    sink = Sink()
    emit_call_events_from_capsule(sink, capsule, run_id="r", agent_id="a")  # type: ignore[arg-type]
    # The retried call's 500 attempt is not a ModelCallFailed of its own.
    assert sink.events.count("ModelCallCompleted") + sink.events.count("ModelCallFailed") == 6
    assert sink.events.count("ModelCallFailed") == 1  # the HTTP 400 call


def test_diff_of_a_capsule_with_itself_has_six_pairs(capsule: Path, tmp_path: Path) -> None:
    from novafabric.diff._engine import DiffEngine

    other = tmp_path / "copy"
    shutil.copytree(capsule, other)
    report = DiffEngine().compare(capsule, other)
    assert len(report.model_call_pairs) == 6
    assert not report.has_changes
