"""ADR-0147 D2/D6 — the ``nova drift collect`` CLI.

The load-bearing tests are the two that pipe this command's output straight into the sibling
detector. Asserting the document's *shape* here would only prove it matches what I believed the
detector wanted; running the detector on it proves the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_SHA = "sha256:" + "a" * 64


def _score(name: str, value: float) -> dict:
    return {
        "score_id": "01HZZZZZZZZZZZZZZZZZZZZZZZ",
        "subject": _SHA,
        "subject_kind": "capsule",
        "name": name,
        "value": value,
        "value_type": "numeric",
        "source": "code",
        "evaluator_id": "test-evaluator",
        "eval_card_digest": _SHA,
    }


def _capsule(
    root: Path, run_id: str, *, created_at: str, cost: float | None, score: float | None
) -> None:
    d = root / run_id
    d.mkdir(parents=True)
    (d / "capsule.json").write_text(
        json.dumps({"run_id": run_id, "created_at": created_at, "status": "success"})
    )
    if cost is not None:
        (d / "model-calls.jsonl").write_text(
            json.dumps(
                {
                    "gen_ai.response.model": "gpt-x",
                    "nova.cost": {"amount": cost, "currency": "EUR"},
                    "gen_ai.usage.input_tokens": 100,
                    "gen_ai.usage.output_tokens": 20,
                }
            )
            + "\n"
        )
    if score is not None:
        (d / "scores.jsonl").write_text(json.dumps(_score("pass-rate", score)) + "\n")


def _store(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    for i, (cost, score) in enumerate([(0.1, 0.9), (0.2, 0.8), (0.3, 0.4)], start=1):
        _capsule(root, f"run-{i}", created_at=f"2026-07-0{i}T00:00:00Z", cost=cost, score=score)
    return root


def _run(store: Path, *args: str):
    return runner.invoke(app, ["drift", "collect", "--capsules", str(store), *args])


# ── runs ─────────────────────────────────────────────────────────────────


def test_runs_is_the_default_emit(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--window", "..", "--json", "--no-cache")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["n"] == 3
    assert {r["run_id"] for r in payload["runs"]} == {"run-1", "run-2", "run-3"}


def test_text_mode_summarises_and_points_at_json(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--window", "..", "--no-cache")
    assert result.exit_code == 0, result.output
    assert "Collected 3 run(s)" in result.output
    assert "--json" in result.output


def test_an_empty_window_is_zero_runs_not_an_error(tmp_path: Path) -> None:
    result = _run(
        _store(tmp_path),
        "--window", "2020-01-01T00:00:00Z..2020-01-02T00:00:00Z", "--json", "--no-cache",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["n"] == 0


# ── the documents feed the detectors ─────────────────────────────────────


def test_the_collected_document_runs_through_drift_detect(tmp_path: Path) -> None:
    """Collect → detect, end to end, with no hand-written document in between."""
    store = _store(tmp_path)
    collected = _run(
        store,
        "--emit", "detect",
        "--baseline", "2026-07-01T00:00:00Z..2026-07-02T00:00:00Z",
        "--window", "2026-07-02T00:00:00Z..",
        "--dimension", "cost", "--statistic", "psi", "--threshold", "0.2",
        "--json", "--no-cache",
    )
    assert collected.exit_code == 0, collected.output

    doc = tmp_path / "drift.json"
    doc.write_text(collected.stdout)
    detected = runner.invoke(app, ["drift", "detect", str(doc), "--json"])
    assert detected.exit_code == 0, detected.output
    assert "drifted" in json.loads(detected.stdout)


def test_the_collected_document_runs_through_silent_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    collected = _run(
        store,
        "--emit", "silent-failure", "--window", "..",
        "--quality-metric", "pass-rate", "--threshold", "0.5",
        "--json", "--no-cache",
    )
    assert collected.exit_code == 0, collected.output

    doc = tmp_path / "silent.json"
    doc.write_text(collected.stdout)
    flagged = runner.invoke(app, ["drift", "silent-failure", str(doc), "--json"])
    assert flagged.exit_code == 0, flagged.output
    report = json.loads(flagged.stdout)
    assert report["silent_failures"] == 1  # run-3 reported success at 0.4


def test_the_collected_document_runs_through_drift_fingerprint(tmp_path: Path) -> None:
    """Collect a trajectory from sealed capsules → fingerprint it, no hand-written document."""
    store = _store(tmp_path)
    for run_id, calls in (
        ("run-1", [{"tool_name": "search", "arguments": {"q": "a"}},
                   {"tool_name": "write", "arguments": {"path": "o"}}]),
        ("run-3", [{"tool_name": "search", "arguments": {"q": "a"}},
                   {"tool_name": "deploy", "arguments": {"env": "prod"}}]),
    ):
        (store / run_id / "tool-calls.jsonl").write_text(
            "\n".join(json.dumps(c) for c in calls) + "\n"
        )

    collected = _run(
        store, "--emit", "fingerprint", "--run", "run-3", "--baseline-run", "run-1",
        "--threshold", "0.2", "--json", "--no-cache",
    )
    assert collected.exit_code == 0, collected.output

    doc = tmp_path / "fp.json"
    doc.write_text(collected.stdout)
    result = runner.invoke(app, ["drift", "fingerprint", str(doc), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["shifted"] is True


def test_fingerprint_text_mode_reports_the_trajectory_length(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (store / "run-1" / "tool-calls.jsonl").write_text(
        json.dumps({"tool_name": "search", "arguments": {}}) + "\n"
    )
    result = _run(store, "--emit", "fingerprint", "--run", "run-1", "--no-cache")
    assert result.exit_code == 0, result.output
    # Flattened: rich wraps on a narrow terminal, so a raw substring match is brittle.
    assert "1 tool call(s)" in " ".join(result.output.split())


def test_the_canonicalizer_flags_reach_the_document(tmp_path: Path) -> None:
    """`--commutable`/`--idempotent` are the caller's declaration and must survive the trip.

    Nothing is assumed commutable (ADR-0144), so a flag that silently vanished would leave the
    fingerprint normalizing less than the caller believed — and looking like real drift.
    """
    store = _store(tmp_path)
    (store / "run-1" / "tool-calls.jsonl").write_text(
        json.dumps({"tool_name": "search", "arguments": {}}) + "\n"
    )
    result = _run(
        store, "--emit", "fingerprint", "--run", "run-1",
        "--idempotent", "search", "--commutable", "read", "--commutable", "write",
        "--json", "--no-cache",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["idempotent"] == ["search"]
    assert payload["commutable"] == ["read", "write"]


def test_fingerprint_without_a_run_is_refused(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--emit", "fingerprint", "--no-cache")
    assert result.exit_code == 2
    assert "--run" in result.output


def test_a_run_with_nothing_to_fingerprint_exits_two(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--emit", "fingerprint", "--run", "run-1", "--no-cache")
    assert result.exit_code == 2
    assert "nothing to fingerprint" in result.output


# ── root-cause reads the lineage store, not the capsule tree ─────────────


def _lineage(tmp_path: Path):
    from novafabric.lineage._store import LineageStore
    from novafabric.lineage._types import LineageEdge

    store = LineageStore(tmp_path / "lineage.db")
    for run, model in (("run-base", "m-2024"), ("run-drift", "m-2025")):
        store.insert_edge(
            LineageEdge(
                edge_type="used",
                source={"kind": "run", "run_id": run},
                target={"kind": "model", "ref": model},
                confidence="declared",
                capsule_run_id=run,
            )
        )
    return tmp_path / "lineage.db"


def test_the_collected_document_runs_through_drift_root_cause(tmp_path: Path) -> None:
    """Collect provenance from the lineage store → root-cause it, no hand-written lists."""
    db = _lineage(tmp_path)
    collected = runner.invoke(app, [
        "drift", "collect", "--emit", "root-cause",
        "--run", "run-drift", "--baseline-run", "run-base",
        "--lineage-db", str(db), "--json",
    ])
    assert collected.exit_code == 0, collected.output

    doc = tmp_path / "rc.json"
    doc.write_text(collected.stdout)
    result = runner.invoke(app, ["drift", "root-cause", str(doc), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["confidence"] == "sole_change"
    assert payload["correlation_only"] is True


def test_root_cause_does_not_need_a_capsule_directory(tmp_path: Path) -> None:
    """It reads the lineage store; failing on a capsule tree it never opens would be wrong."""
    db = _lineage(tmp_path)
    result = runner.invoke(app, [
        "drift", "collect", "--emit", "root-cause",
        "--capsules", str(tmp_path / "does-not-exist"),
        "--run", "run-drift", "--baseline-run", "run-base",
        "--lineage-db", str(db), "--json",
    ])
    assert result.exit_code == 0, result.output


def test_a_run_absent_from_the_lineage_graph_exits_two(tmp_path: Path) -> None:
    db = _lineage(tmp_path)
    result = runner.invoke(app, [
        "drift", "collect", "--emit", "root-cause",
        "--run", "run-ghost", "--baseline-run", "run-base",
        "--lineage-db", str(db), "--json",
    ])
    assert result.exit_code == 2
    assert "not in the lineage graph" in result.output


def test_root_cause_without_both_runs_is_refused(tmp_path: Path) -> None:
    db = _lineage(tmp_path)
    result = runner.invoke(app, [
        "drift", "collect", "--emit", "root-cause", "--run", "run-drift",
        "--lineage-db", str(db), "--json",
    ])
    assert result.exit_code == 2
    assert "--baseline-run" in result.output


def test_root_cause_text_mode_reports_the_depth_and_counts(tmp_path: Path) -> None:
    db = _lineage(tmp_path)
    result = runner.invoke(app, [
        "drift", "collect", "--emit", "root-cause",
        "--run", "run-drift", "--baseline-run", "run-base",
        "--lineage-db", str(db), "--depth", "3",
    ])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "depth 3" in flat
    assert "1 ancestor(s)" in flat


# ── policy is never defaulted, and bad input exits 2 ─────────────────────


def test_detect_without_a_threshold_is_refused(tmp_path: Path) -> None:
    result = _run(
        _store(tmp_path),
        "--emit", "detect", "--baseline", "..", "--window", "..",
        "--dimension", "cost", "--statistic", "psi", "--json", "--no-cache",
    )
    assert result.exit_code == 2
    assert "--threshold" in result.output


def test_silent_failure_without_a_quality_metric_is_refused(tmp_path: Path) -> None:
    result = _run(
        _store(tmp_path), "--emit", "silent-failure", "--window", "..",
        "--threshold", "0.5", "--json", "--no-cache",
    )
    assert result.exit_code == 2
    assert "--quality-metric" in result.output


def test_an_unknown_dimension_exits_two(tmp_path: Path) -> None:
    result = _run(
        _store(tmp_path),
        "--emit", "detect", "--baseline", "..", "--window", "..",
        "--dimension", "cost-per-token", "--statistic", "psi", "--threshold", "0.2",
        "--json", "--no-cache",
    )
    assert result.exit_code == 2
    assert "unknown dimension" in result.output


def test_a_duration_in_until_exits_two_rather_than_traceback(tmp_path: Path) -> None:
    """`--window 30d..7d` is the obvious thing to write; it must fail cleanly, not crash."""
    result = _run(_store(tmp_path), "--window", "30d..7d", "--json", "--no-cache")
    assert result.exit_code == 2
    assert "timestamp only" in result.output


def test_a_malformed_window_spec_exits_two(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--window", "7d", "--json", "--no-cache")
    assert result.exit_code == 2
    assert "SINCE..UNTIL" in result.output


def test_an_unknown_emit_exits_two(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--window", "..", "--emit", "trajectory", "--no-cache")
    assert result.exit_code == 2


def test_a_missing_capsule_directory_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["drift", "collect", "--capsules", str(tmp_path / "nope"), "--window", ".."]
    )
    assert result.exit_code == 2


def test_help_smoke() -> None:
    result = runner.invoke(app, ["drift", "collect", "--help"])
    assert result.exit_code == 0
    assert "collect" in result.output.lower()
