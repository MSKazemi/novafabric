"""NF-169 — the ``nova toolschema deprecations`` CLI (ADR-0148 D2).

Reporting, not gating: exit ``0`` whether or not runs are pinned, ``2`` only on bad input. The
`unknown`-version bucket must survive into the output — a run that cannot be judged must not
render as one that was cleared.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

WHEN = "2026-07-28T00:00:00Z"


def _capsule(root: Path, run_id: str, calls: list[tuple[str, str]]) -> None:
    d = root / run_id
    d.mkdir(parents=True)
    d.joinpath("capsule.json").write_text(
        json.dumps({"run_id": run_id, "created_at": "2026-07-10T00:00:00Z", "status": "success"})
    )
    d.joinpath("tool-calls.jsonl").write_text(
        "\n".join(
            json.dumps({"tool_name": n, "tool_version": v, "arguments": {}}) for n, v in calls
        )
        + "\n"
    )


def _store(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(root, "run-old", [("search", "1.0.0")])
    _capsule(root, "run-new", [("search", "2.0.0")])
    _capsule(root, "run-unknown", [("search", "unknown")])
    return root


def _run(store: Path, *extra: str, version: str = "1.0.0"):
    return runner.invoke(app, [
        "toolschema", "deprecations", "--capsules", str(store),
        "--tool", "search", "--version", version, "--deprecated-at", WHEN, *extra,
    ])


def test_pinned_runs_are_flagged_and_it_still_exits_zero(tmp_path: Path) -> None:
    """It reports; it does not gate."""
    result = _run(_store(tmp_path))
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "pinned run-old" in flat
    assert "1 run(s) pinned to it, of 3 capsule(s) scanned" in flat


def test_an_unknown_version_is_shown_as_unknown_not_cleared(tmp_path: Path) -> None:
    flat = " ".join(_run(_store(tmp_path)).output.split())
    assert "unknown version run-unknown" in flat
    assert "neither confirmed nor cleared" in flat


def test_json_carries_the_three_buckets_and_the_scan_count(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dependent_run_ids"] == ["run-old"]
    assert payload["unknown_version_run_ids"] == ["run-unknown"]
    assert payload["capsules_scanned"] == 3
    assert "successor" not in payload  # absent, not empty


def test_no_pinned_runs_still_reports_what_was_searched(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--json", version="9.9.9")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["dependent_run_ids"] == []
    assert payload["capsules_scanned"] == 3, "an empty answer must say what it searched"


def test_a_named_successor_is_rendered(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), "--successor", "mcp://acme/search@2")
    assert result.exit_code == 0, result.output
    assert "mcp://acme/search@2" in " ".join(result.output.split())


def test_deprecating_the_unknown_version_exits_two(tmp_path: Path) -> None:
    result = _run(_store(tmp_path), version="unknown")
    assert result.exit_code == 2
    assert "cannot deprecate version" in result.output


def test_an_unparseable_date_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "toolschema", "deprecations", "--capsules", str(_store(tmp_path)),
        "--tool", "search", "--version", "1.0.0", "--deprecated-at", "last tuesday",
    ])
    assert result.exit_code == 2
    assert "ISO-8601" in result.output


def test_a_missing_capsule_directory_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "toolschema", "deprecations", "--capsules", str(tmp_path / "nope"),
        "--tool", "search", "--version", "1.0.0", "--deprecated-at", WHEN,
    ])
    assert result.exit_code == 2


def test_help_smoke() -> None:
    result = runner.invoke(app, ["toolschema", "deprecations", "--help"])
    assert result.exit_code == 0
    assert "retired" in result.output.lower()
