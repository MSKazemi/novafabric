"""CLI tests for `nova view` (ADR-0130) — save/list/show/run/rm, I2/I3 invariants."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

CapsuleFactory = Callable[..., Path]
RecordFactory = Callable[..., dict[str, Any]]


def _save(views_dir: Path, *extra: str) -> Any:
    return runner.invoke(
        app,
        [
            "view", "save", "avg-cost",
            "--select", "avg(cost) AS avg_cost, count() AS runs",
            "--where", "model = m1",
            "--views-dir", str(views_dir),
            *extra,
        ],
    )


def test_view_help() -> None:
    result = runner.invoke(app, ["view", "--help"])
    assert result.exit_code == 0
    for sub in ("save", "run", "list", "show", "rm"):
        assert sub in result.output


def test_save_writes_yaml_and_reports_hash(views_dir: Path) -> None:
    result = _save(views_dir)
    assert result.exit_code == 0, result.output
    assert "Saved view 'avg-cost'" in result.output
    assert "view_hash: sha256:" in result.output
    document = yaml.safe_load((views_dir / "avg-cost.yaml").read_text())
    assert document["query"]["select"] == ["avg(cost) AS avg_cost", "count() AS runs"]
    assert document["query"]["where"] == ["model = m1"]


def test_save_json_file(views_dir: Path) -> None:
    result = _save(views_dir, "--json")
    assert result.exit_code == 0, result.output
    assert json.loads((views_dir / "avg-cost.json").read_text())["view_id"] == "avg-cost"


def test_save_invalid_query_exits_two_nothing_written(views_dir: Path) -> None:
    result = runner.invoke(
        app,
        ["view", "save", "bad", "--select", "median(cost)", "--views-dir", str(views_dir)],
    )
    assert result.exit_code == 2
    assert "median" in result.output
    assert not views_dir.exists()


def test_save_bad_view_id_exits_two(views_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "view", "save", "x",
            "--select", "count()",
            "--view-id", "Not A Slug",
            "--views-dir", str(views_dir),
        ],
    )
    assert result.exit_code == 2
    assert not views_dir.exists()


def test_save_collision_refused_with_hint(views_dir: Path) -> None:
    assert _save(views_dir).exit_code == 0
    # A different name that slugs to the same view_id collides.
    result = runner.invoke(
        app,
        [
            "view", "save", "Avg Cost!",
            "--select", "count()",
            "--views-dir", str(views_dir),
        ],
    )
    assert result.exit_code == 1
    assert_flag_in_help(result, "--force") or "--view-id" in result.output


def test_save_force_overwrites_preserving_created_at(views_dir: Path) -> None:
    assert _save(views_dir).exit_code == 0
    created = yaml.safe_load((views_dir / "avg-cost.yaml").read_text())["created_at"]
    result = _save(views_dir, "--force", "--description", "v2")
    assert result.exit_code == 0, result.output
    document = yaml.safe_load((views_dir / "avg-cost.yaml").read_text())
    assert document["created_at"] == created
    assert document["description"] == "v2"
    assert "updated_at" in document


def test_run_matches_nova_query_exactly(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    views_dir: Path,
    model_call: RecordFactory,
) -> None:
    """Invariant I2: `nova view run` is exactly `nova query` over the stored query."""
    make_capsule("run-1", model_calls=[model_call(model="m1", cost=0.02)])
    make_capsule("run-2", model_calls=[model_call(model="m1", cost=0.04)])
    make_capsule("run-3", model_calls=[model_call(model="m2", cost=0.10)])
    assert _save(views_dir).exit_code == 0

    view_result = runner.invoke(
        app,
        [
            "view", "run", "avg-cost",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
            "--json",
        ],
    )
    assert view_result.exit_code == 0, view_result.output
    view_payload = json.loads(view_result.output)

    query_result = runner.invoke(
        app,
        [
            "query",
            "--select", "avg(cost) AS avg_cost, count() AS runs",
            "--where", "model = m1",
            "--capsule-dir", str(capsule_dir),
            "--json",
        ],
    )
    query_payload = json.loads(query_result.output)

    for key in ("columns", "rows", "row_count", "truncated", "query"):
        assert view_payload[key] == query_payload[key], key
    assert view_payload["rows"] == [{"avg_cost": 0.03, "runs": 2}]
    assert view_payload["view"]["view_id"] == "avg-cost"
    assert view_payload["view"]["view_hash"].startswith("sha256:")


def test_run_is_deterministic_across_reruns(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    views_dir: Path,
    model_call: RecordFactory,
) -> None:
    make_capsule("run-1", model_calls=[model_call(model="m1", cost=0.02)])
    assert _save(views_dir).exit_code == 0
    args = [
        "view", "run", "avg-cost",
        "--views-dir", str(views_dir),
        "--capsule-dir", str(capsule_dir),
        "--json",
    ]
    first = json.loads(runner.invoke(app, args).output)
    second = json.loads(runner.invoke(app, args).output)
    for key in ("columns", "rows", "row_count", "truncated", "query", "view"):
        assert first[key] == second[key], key


def test_run_unknown_view_exits_one(views_dir: Path, capsule_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "view", "run", "nope",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
        ],
    )
    assert result.exit_code == 1
    assert "nope" in result.output


def test_run_corrupt_view_exits_one(views_dir: Path, capsule_dir: Path) -> None:
    views_dir.mkdir(parents=True)
    (views_dir / "broken.yaml").write_text("{ not: [valid")
    result = runner.invoke(
        app,
        [
            "view", "run", "broken",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
        ],
    )
    assert result.exit_code == 1


def test_display_format_advisory_cli_overrides(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    views_dir: Path,
    model_call: RecordFactory,
) -> None:
    """Invariant I3: display prefs are advisory; a CLI flag wins."""
    make_capsule("run-1", model_calls=[model_call(model="m1", cost=0.02)])
    assert _save(views_dir, "--format", "json").exit_code == 0

    # Saved display.format=json applies when no flag is given...
    as_saved = runner.invoke(
        app,
        [
            "view", "run", "avg-cost",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
        ],
    )
    assert as_saved.exit_code == 0, as_saved.output
    assert json.loads(as_saved.output)["rows"] == [{"avg_cost": 0.02, "runs": 1}]

    # ...and --format table on the command line overrides it.
    overridden = runner.invoke(
        app,
        [
            "view", "run", "avg-cost",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
            "--format", "table",
        ],
    )
    assert overridden.exit_code == 0, overridden.output
    assert "avg_cost" in overridden.output
    assert "row(s)" in overridden.output


def test_display_columns_affect_table_not_json(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    views_dir: Path,
    model_call: RecordFactory,
) -> None:
    make_capsule("run-1", model_calls=[model_call(model="m1", cost=0.02)])
    assert _save(views_dir, "--columns", "runs").exit_code == 0
    table = runner.invoke(
        app,
        [
            "view", "run", "avg-cost",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
        ],
    )
    assert table.exit_code == 0, table.output
    header = table.output.splitlines()[0]
    assert "runs" in header and "avg_cost" not in header
    # The canonical JSON result is untouched by display prefs.
    payload = json.loads(
        runner.invoke(
            app,
            [
                "view", "run", "avg-cost",
                "--views-dir", str(views_dir),
                "--capsule-dir", str(capsule_dir),
                "--json",
            ],
        ).output
    )
    assert payload["columns"] == ["avg_cost", "runs"]


def test_run_csv_format(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    views_dir: Path,
    model_call: RecordFactory,
) -> None:
    make_capsule("run-1", model_calls=[model_call(model="m1", cost=0.02)])
    assert _save(views_dir).exit_code == 0
    result = runner.invoke(
        app,
        [
            "view", "run", "avg-cost",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
            "--format", "csv",
        ],
    )
    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert lines[0] == "avg_cost,runs"
    assert lines[1] == "0.02,1"


def test_list_empty_exits_zero(views_dir: Path) -> None:
    result = runner.invoke(app, ["view", "list", "--views-dir", str(views_dir)])
    assert result.exit_code == 0
    assert "(no views)" in result.output


def test_list_shows_views_and_skips_corrupt(views_dir: Path) -> None:
    assert _save(views_dir, "--tags", "triage,cost").exit_code == 0
    views_dir.joinpath("broken.yaml").write_text(":::")
    result = runner.invoke(app, ["view", "list", "--views-dir", str(views_dir)])
    assert result.exit_code == 0, result.output
    assert "avg-cost" in result.output
    assert "triage, cost" in result.output
    json_result = runner.invoke(app, ["view", "list", "--views-dir", str(views_dir), "--json"])
    payload = json.loads(json_result.stdout)
    assert payload[0]["view_id"] == "avg-cost"
    assert payload[0]["view_hash"].startswith("sha256:")


def test_show_prints_view_and_hash(views_dir: Path) -> None:
    assert _save(views_dir).exit_code == 0
    result = runner.invoke(app, ["view", "show", "avg-cost", "--views-dir", str(views_dir)])
    assert result.exit_code == 0, result.output
    assert "view_id: avg-cost" in result.output
    assert "# view_hash: sha256:" in result.output
    json_result = runner.invoke(
        app, ["view", "show", "avg-cost", "--views-dir", str(views_dir), "--json"]
    )
    payload = json.loads(json_result.output)
    assert payload["view"]["view_id"] == "avg-cost"
    assert payload["view_hash"].startswith("sha256:")
    assert payload["path"].endswith("avg-cost.yaml")


def test_show_unknown_exits_one(views_dir: Path) -> None:
    result = runner.invoke(app, ["view", "show", "nope", "--views-dir", str(views_dir)])
    assert result.exit_code == 1


def test_rm_removes_view(views_dir: Path) -> None:
    assert _save(views_dir).exit_code == 0
    result = runner.invoke(app, ["view", "rm", "avg-cost", "--views-dir", str(views_dir)])
    assert result.exit_code == 0, result.output
    assert not (views_dir / "avg-cost.yaml").exists()


def test_rm_unknown_exits_one(views_dir: Path) -> None:
    result = runner.invoke(app, ["view", "rm", "nope", "--views-dir", str(views_dir)])
    assert result.exit_code == 1


def test_saved_definition_hash_stable_across_saves(views_dir: Path, tmp_path: Path) -> None:
    """Same definition saved twice (different times/dirs) hashes identically."""
    first = _save(views_dir)
    other_dir = tmp_path / "views-b"
    second = _save(other_dir)
    def hash_line(out: str) -> str:
        return next(line for line in out.splitlines() if "view_hash" in line)

    assert hash_line(first.output) == hash_line(second.output)


def test_save_bad_format_exits_two(views_dir: Path) -> None:
    result = _save(views_dir, "--format", "pdf")
    assert result.exit_code == 2
    assert "pdf" in result.output
    assert not views_dir.exists()


def test_save_bad_sort_entry_exits_two(views_dir: Path) -> None:
    result = _save(views_dir, "--sort", "avg_cost sideways")
    assert result.exit_code == 2
    assert not views_dir.exists()


def test_display_sort_reorders_table_rendering_only(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    views_dir: Path,
    model_call: RecordFactory,
) -> None:
    """Advisory display sort (I3) reorders the table; canonical JSON is untouched."""
    make_capsule("run-1", model_calls=[model_call(model="m1", cost=0.02)])
    make_capsule("run-2", model_calls=[model_call(model="m2", cost=0.08)])
    save = runner.invoke(
        app,
        [
            "view", "save", "by-model",
            "--select", "avg(cost) AS avg_cost",
            "--group-by", "model",
            "--order-by", "avg_cost desc",
            "--sort", "model asc",
            "--views-dir", str(views_dir),
        ],
    )
    assert save.exit_code == 0, save.output
    table = runner.invoke(
        app,
        [
            "view", "run", "by-model",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
        ],
    )
    assert table.exit_code == 0, table.output
    lines = [line for line in table.output.splitlines() if line.startswith(("m1", "m2"))]
    assert lines[0].startswith("m1") and lines[1].startswith("m2")
    payload = json.loads(
        runner.invoke(
            app,
            [
                "view", "run", "by-model",
                "--views-dir", str(views_dir),
                "--capsule-dir", str(capsule_dir),
                "--json",
            ],
        ).output
    )
    # Canonical rows keep the stored query's own order_by (avg_cost desc).
    assert [row["model"] for row in payload["rows"]] == ["m2", "m1"]


def test_run_bad_format_flag_exits_two(views_dir: Path, capsule_dir: Path) -> None:
    assert _save(views_dir).exit_code == 0
    result = runner.invoke(
        app,
        [
            "view", "run", "avg-cost",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
            "--format", "xml",
        ],
    )
    assert result.exit_code == 2


def test_run_stale_stored_query_surfaces_parse_error(
    views_dir: Path, capsule_dir: Path
) -> None:
    """An envelope-valid view whose query no longer parses fails loudly, exit 2."""
    views_dir.mkdir(parents=True)
    (views_dir / "stale.yaml").write_text(
        "schema_version: '0.1.0'\n"
        "view_id: stale\n"
        "name: stale\n"
        "created_at: '2026-07-15T10:00:00Z'\n"
        "query:\n  select:\n    - median(cost)\n"
    )
    result = runner.invoke(
        app,
        [
            "view", "run", "stale",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
        ],
    )
    assert result.exit_code == 2
    assert "median" in result.output


def test_run_missing_capsule_dir_exits_one(views_dir: Path, tmp_path: Path) -> None:
    assert _save(views_dir).exit_code == 0
    result = runner.invoke(
        app,
        [
            "view", "run", "avg-cost",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(tmp_path / "nope"),
        ],
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_run_empty_capsule_dir_exits_zero(
    views_dir: Path, capsule_dir: Path
) -> None:
    assert _save(views_dir).exit_code == 0
    result = runner.invoke(
        app,
        [
            "view", "run", "avg-cost",
            "--views-dir", str(views_dir),
            "--capsule-dir", str(capsule_dir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rows"] == []
