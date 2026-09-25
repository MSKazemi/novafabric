"""CLI tests for `nova diff --graph-shape / --assert-same-shape` (ADR-0124 P3, diff half)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

import novafabric.cli.diff as cli_diff
import novafabric.diff.graph_shape as gs
from novafabric.cli.main import app
from novafabric.diff._engine import DiffEngine
from novafabric.diff._format import format_github_annotations, format_json, format_text

runner = CliRunner()


def _capsule(root: Path, name: str, run_id: str, tools: tuple[str, ...]) -> Path:
    cap = root / name
    (cap / "outputs").mkdir(parents=True)
    (cap / "inputs").mkdir()
    (cap / "capsule.yaml").write_text(
        yaml.dump({"schema_version": "0.1.0", "run_id": run_id, "status": "success"})
    )
    (cap / "env.lock").write_text(yaml.dump({"python": {"version": "3.12.3"}}))
    (cap / "outputs" / "stdout.txt").write_text("output\n")
    (cap / "trace.jsonl").write_text(
        json.dumps({"span_id": f"{run_id}-s", "parent_span_id": None, "name": "agent.turn"}) + "\n"
    )
    (cap / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "model_call_id": f"{run_id}-m",
                "parent_span_id": f"{run_id}-s",
                "gen_ai.request.model": "m-1",
                "span_id": f"{run_id}-s",
            }
        )
        + "\n"
    )
    (cap / "tool-calls.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "tool_call_id": f"{run_id}-t{i}",
                    "parent_span_id": f"{run_id}-s",
                    "agent_call_id": f"{run_id}-m",
                    "tool_name": tool,
                    "started_at": f"2026-05-07T10:00:00.{i:03d}Z",
                }
            )
            + "\n"
            for i, tool in enumerate(tools)
        )
    )
    return cap


@pytest.fixture()
def pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    a = _capsule(tmp_path, "a", "RUNA", ("web_search", "read_file"))
    b = _capsule(tmp_path, "b", "RUNB", ("web_search", "read_file"))
    c = _capsule(tmp_path, "c", "RUNC", ("web_search", "read_file", "git"))
    return a, b, c


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_diff, "console", Console(width=400))


def _squash(text: str) -> str:
    return " ".join(text.split())


# --- default output is byte-identical without the flags ---------------------------------


@pytest.mark.parametrize("fmt", ["text", "json", "github-annotation"])
def test_default_output_byte_identical_without_flags(
    pair: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, fmt: str
) -> None:
    a, _, c = pair

    def _must_not_run(*_: object, **__: object) -> None:
        raise AssertionError("graph shape computed without --graph-shape")

    monkeypatch.setattr(gs, "compare_graph_shapes", _must_not_run)
    result = runner.invoke(app, ["diff", str(a), str(c), "--output-format", fmt])
    assert result.exit_code == 0, result.output

    report = DiffEngine().compare(a, c)
    expected = {
        "text": format_text(report),
        "json": format_json(report),
        "github-annotation": format_github_annotations(report),
    }[fmt]
    assert result.output == expected + "\n"
    assert "graph_shape" not in result.output and "Graph shape" not in result.output


def test_flag_only_appends_block(pair: tuple[Path, Path, Path]) -> None:
    a, _, c = pair
    plain = runner.invoke(app, ["diff", str(a), str(c)]).output
    shaped = runner.invoke(app, ["diff", str(a), str(c), "--graph-shape"]).output
    assert shaped.startswith(plain)
    assert "Graph shape (ADR-0124, experimental): shape changed" in shaped[len(plain) :]


def test_json_graph_shape_is_additive(pair: tuple[Path, Path, Path]) -> None:
    a, _, c = pair
    plain = json.loads(
        runner.invoke(app, ["diff", str(a), str(c), "--output-format", "json"]).output
    )
    result = runner.invoke(
        app, ["diff", str(a), str(c), "--output-format", "json", "--graph-shape"]
    )
    assert result.exit_code == 0
    doc = json.loads(result.output)
    block = doc.pop("graph_shape")
    assert doc == plain
    assert block["status"] == "shape_changed"
    assert block["counts"]["nodes_added"] == 1
    assert block["nodes_added"][0]["label"] == "git"
    assert block["a"]["graph_digest"].startswith("sha256:")


# --- text + annotations ---------------------------------------------------------------------


def test_text_same_shape(pair: tuple[Path, Path, Path]) -> None:
    a, b, _ = pair
    result = runner.invoke(app, ["diff", str(a), str(b), "--graph-shape"])
    assert result.exit_code == 0
    assert "Graph shape (ADR-0124, experimental): same shape" in result.output
    assert "equal shape_digest" in result.output


def test_github_annotation_with_flag(pair: tuple[Path, Path, Path]) -> None:
    a, _, c = pair
    result = runner.invoke(
        app, ["diff", str(a), str(c), "--output-format", "github-annotation", "--graph-shape"]
    )
    assert result.exit_code == 0
    assert "::error title=NovaFabric Graph Shape::Agent graph shape changed" in result.output


def test_group_by_variant_json_carries_block(pair: tuple[Path, Path, Path]) -> None:
    a, b, _ = pair
    result = runner.invoke(
        app,
        [
            "diff",
            str(a),
            str(b),
            "--group-by",
            "variant",
            "--output-format",
            "json",
            "--graph-shape",
        ],
    )
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert set(doc) == {"variant_groups", "cross_arm", "diff", "graph_shape"}
    assert doc["graph_shape"]["status"] == "same_shape"


# --- exit codes -----------------------------------------------------------------------------


def test_assert_same_shape_exit_0_when_same(pair: tuple[Path, Path, Path]) -> None:
    a, b, _ = pair
    result = runner.invoke(app, ["diff", str(a), str(b), "--assert-same-shape"])
    assert result.exit_code == 0
    assert "same shape" in result.output  # implies --graph-shape


def test_assert_same_shape_exit_1_on_mismatch(pair: tuple[Path, Path, Path]) -> None:
    a, _, c = pair
    result = runner.invoke(app, ["diff", str(a), str(c), "--assert-same-shape"])
    assert result.exit_code == 1
    squashed = _squash(result.output)
    assert "+ tool_call span:agent.turn[0]/tool_call:git[0] (id RUNC-t2)" in squashed


def test_graph_shape_alone_never_gates(pair: tuple[Path, Path, Path]) -> None:
    a, _, c = pair
    assert runner.invoke(app, ["diff", str(a), str(c), "--graph-shape"]).exit_code == 0


def test_malformed_capsule_reports_unavailable_and_diff_survives(
    pair: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, _ = pair

    def boom(_: Path) -> None:
        raise ValueError("corrupt trace")

    monkeypatch.setattr(gs, "build_agent_graph", boom)
    result = runner.invoke(app, ["diff", str(a), str(b), "--graph-shape"])
    assert result.exit_code == 0
    assert "Diff: RUNA" in result.output
    assert "graph unavailable" in result.output and "ValueError: corrupt trace" in result.output

    gated = runner.invoke(app, ["diff", str(a), str(b), "--assert-same-shape"])
    assert gated.exit_code == 2  # fail closed: shape cannot be verified


def test_regression_gate_takes_precedence(pair: tuple[Path, Path, Path]) -> None:
    a, _, c = pair
    (c / "outputs" / "stdout.txt").write_text("different\n")
    result = runner.invoke(
        app, ["diff", str(a), str(c), "--assert-no-regressions", "--assert-same-shape"]
    )
    assert result.exit_code == 1


# --- flag validation ----------------------------------------------------------------------


def test_rejected_for_asset_refs() -> None:
    result = runner.invoke(app, ["diff", "x@1", "x@2", "--graph-shape"])
    assert result.exit_code == 2
    assert "capsule diffs only" in _squash(result.output)


@pytest.mark.parametrize("mode", ["--media", "--significance"])
def test_rejected_with_other_modes(pair: tuple[Path, Path, Path], mode: str) -> None:
    a, b, _ = pair
    result = runner.invoke(app, ["diff", str(a), str(b), mode, "--assert-same-shape"])
    assert result.exit_code == 2
    assert "--significance" in _squash(result.output).split("Error", 1)[1]


def test_help_lists_flags() -> None:
    result = runner.invoke(app, ["diff", "--help"], terminal_width=200)
    assert result.exit_code == 0
    assert "--graph-shape" in result.output and "--assert-same-shape" in result.output
