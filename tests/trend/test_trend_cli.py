"""`nova trend` CLI tests (ADR-0131) — output modes, exit codes, view wiring."""

from __future__ import annotations

import json
from pathlib import Path

from trend.conftest import CapsuleFactory, RecordFactory
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.views.model import SavedView
from novafabric.views.store import save_view

runner = CliRunner()

_WINDOW = ["--since", "2026-07-09T00:00:00Z", "--until", "2026-07-13T00:00:00Z"]


def _seed(make_capsule: CapsuleFactory, model_call: RecordFactory) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z",
                 metadata={"asset": "alpha"}, model_calls=[model_call(cost=0.02)])
    make_capsule("run-b", created_at="2026-07-12T08:00:00Z",
                 metadata={"asset": "beta"}, model_calls=[model_call(cost=0.03)])


def test_default_output_is_json_to_stdout(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    _seed(make_capsule, model_call)
    result = runner.invoke(
        app,
        ["trend", "--metric", "cost", *_WINDOW, "--capsule-dir", str(capsule_dir)],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["metric"] == "cost"
    assert [p["bucket"] for p in report["series"]] == [
        "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"
    ]


def test_json_and_html_files_written_instead_of_stdout(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    tmp_path: Path,
) -> None:
    _seed(make_capsule, model_call)
    json_out = tmp_path / "artifacts" / "trend.json"
    html_out = tmp_path / "artifacts" / "trend.html"
    result = runner.invoke(
        app,
        [
            "trend", "--metric", "cost", *_WINDOW,
            "--capsule-dir", str(capsule_dir),
            "--json", str(json_out), "--html", str(html_out),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(json_out.read_text())
    assert report["metric"] == "cost"
    html = html_out.read_text()
    assert "trend-report-data" in html
    assert "https://" not in html  # self-contained artifact
    assert f"Wrote {html_out}" in result.stdout
    assert f"Wrote {json_out}" in result.stdout
    assert "schema_version" not in result.stdout  # no JSON dump when files written


def test_stat_on_non_latency_is_usage_error(capsule_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["trend", "--metric", "cost", "--stat", "p99",
         "--capsule-dir", str(capsule_dir)],
    )
    assert result.exit_code == 2
    assert "only valid with --metric latency" in result.output


def test_unknown_metric_is_usage_error(capsule_dir: Path) -> None:
    result = runner.invoke(
        app, ["trend", "--metric", "tokens", "--capsule-dir", str(capsule_dir)]
    )
    assert result.exit_code == 2
    assert "unknown metric" in result.output


def test_missing_capsule_dir_is_exit_1(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["trend", "--metric", "cost", "--capsule-dir", str(tmp_path / "nope")]
    )
    assert result.exit_code == 1
    assert "capsule directory not found" in result.output


def test_empty_capsule_dir_succeeds(capsule_dir: Path) -> None:
    result = runner.invoke(
        app, ["trend", "--metric", "cost", "--capsule-dir", str(capsule_dir)]
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["series"] == []
    assert report["capsule_count"] == 0


def test_view_option_selects_capsules(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    views_dir: Path,
) -> None:
    _seed(make_capsule, model_call)
    save_view(
        SavedView(
            view_id="alpha-only",
            name="Alpha only",
            query={"select": ["count()"], "where": ["asset = alpha"]},
            created_at="2026-07-14T00:00:00Z",
        ),
        views_dir,
    )
    result = runner.invoke(
        app,
        [
            "trend", "--metric", "cost", *_WINDOW,
            "--capsule-dir", str(capsule_dir),
            "--view", "alpha-only", "--views-dir", str(views_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["view"] == "alpha-only"
    assert report["capsule_count"] == 1


def test_unknown_view_is_exit_1(capsule_dir: Path, views_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["trend", "--metric", "cost", "--capsule-dir", str(capsule_dir),
         "--view", "missing", "--views-dir", str(views_dir)],
    )
    assert result.exit_code == 1
    assert "no saved view named" in result.output


def test_help_smoke() -> None:
    result = runner.invoke(app, ["trend", "--help"])
    assert result.exit_code == 0
    assert "--metric" in result.output
    assert "--html" in result.output
