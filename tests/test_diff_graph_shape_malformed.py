"""``--graph-shape`` on malformed graph sources: counted, warned, and "cannot compare" under the gate.

The agent-graph builder (ADR-0124) reads ``model-calls.jsonl``,
``tool-calls.jsonl`` and ``trace.jsonl`` best-effort: a line that is not JSON,
or JSON that is not an object, is dropped without a word. So
``--assert-same-shape`` certified "same shape" (exit 0) over records it never
read, and could report a shape change (exit 1) that was only the skipped line.
That is the fail-open ADR-0303 Amendment 1 closed for the record diff.

ADR-0303 Amendment 2: each available side reports the lines its graph
reconstruction skipped, per source file, in ``graph_shape.<side>.skipped_malformed_lines``;
they are warned about on stderr in every format and shown in the text block and
annotations; ``--assert-same-shape`` exits 2 when any count is non-zero, checked
before the shape verdict. Without the gate ``--graph-shape`` still exits 0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from _help_assert import strip_ansi
from rich.console import Console
from typer.testing import CliRunner

import novafabric.cli.diff as cli_diff
from novafabric.cli.main import app
from novafabric.diff.graph_shape import compare_graph_shapes, format_graph_shape_text

runner = CliRunner()

ZERO = {"model_calls": 0, "tool_calls": 0, "trace": 0}


def _capsule(root: Path, name: str, run_id: str, tools: tuple[str, ...]) -> Path:
    cap = root / name
    (cap / "outputs").mkdir(parents=True)
    (cap / "capsule.yaml").write_text(
        yaml.dump({"schema_version": "0.1.0", "run_id": run_id, "status": "success"})
    )
    (cap / "env.lock").write_text(yaml.dump({"python": {"version": "3.12.3"}}))
    (cap / "trace.jsonl").write_text(
        json.dumps({"span_id": f"{run_id}-s", "parent_span_id": None, "name": "agent.turn"})
        + "\n"
    )
    (cap / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "model_call_id": f"{run_id}-m",
                "parent_span_id": f"{run_id}-s",
                "gen_ai.request.model": "m-1",
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
                    "tool_name": tool,
                }
            )
            + "\n"
            for i, tool in enumerate(tools)
        )
    )
    return cap


def _corrupt_trace(cap: Path) -> None:
    """Append two lines the builder cannot use to trace.jsonl (no record-diff file)."""
    with (cap / "trace.jsonl").open("a") as fh:
        fh.write("{not json\n[1, 2]\n")


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_diff, "console", Console(width=400))


@pytest.fixture()
def same(tmp_path: Path) -> tuple[Path, Path]:
    """Same shape over the parsed records; B's trace.jsonl has two unusable lines."""
    a = _capsule(tmp_path, "a", "RUNA", ("web_search",))
    b = _capsule(tmp_path, "b", "RUNB", ("web_search",))
    _corrupt_trace(b)
    return a, b


# ── the comparison ───────────────────────────────────────────────────────────


def test_clean_sides_report_zero_counts(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", "RUNA", ("web_search",))
    b = _capsule(tmp_path, "b", "RUNB", ("web_search",))
    diff = compare_graph_shapes(a, b)
    assert diff.status == "same_shape"
    assert diff.a.skipped_malformed_lines == ZERO
    assert diff.b.skipped_malformed_lines == ZERO
    assert diff.is_complete


def test_skipped_lines_are_counted_per_side_and_source_file(
    same: tuple[Path, Path],
) -> None:
    a, b = same
    with (b / "tool-calls.jsonl").open("a") as fh:
        fh.write('"a string"\n')
    diff = compare_graph_shapes(a, b)
    assert diff.a.skipped_malformed_lines == ZERO
    assert diff.b.skipped_malformed_lines == {"model_calls": 0, "tool_calls": 1, "trace": 2}
    assert not diff.is_complete
    # The best-effort shape is still reported: it locates the damage.
    assert diff.status == "same_shape"
    doc = diff.to_document()
    assert doc["b"]["skipped_malformed_lines"]["trace"] == 2


def test_an_unavailable_side_reports_no_counts_not_zeros(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", "RUNA", ("web_search",))
    missing = tmp_path / "nope"
    diff = compare_graph_shapes(a, missing)
    assert diff.status == "unavailable"
    assert diff.b.skipped_malformed_lines == {}


def test_an_unreadable_source_file_makes_the_side_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The builder read an unreadable file as empty, so "same shape" was certified."""
    a = _capsule(tmp_path, "a", "RUNA", ())
    b = _capsule(tmp_path, "b", "RUNB", ("web_search",))
    real_read_text = Path.read_text

    def _denied(self: Path, *args: object, **kwargs: object) -> str:
        if self == b / "tool-calls.jsonl":
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _denied)
    diff = compare_graph_shapes(a, b)
    assert diff.status == "unavailable"
    assert diff.b.reason is not None and "Permission denied" in diff.b.reason


def test_a_non_utf8_source_file_makes_the_side_unavailable(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", "RUNA", ())
    b = _capsule(tmp_path, "b", "RUNB", ())
    (b / "tool-calls.jsonl").write_bytes(b"\xff\xfe not utf-8\n")
    diff = compare_graph_shapes(a, b)
    assert diff.status == "unavailable"
    assert diff.b.reason is not None and "UnicodeDecodeError" in diff.b.reason


def test_text_block_names_the_skipped_lines(same: tuple[Path, Path]) -> None:
    text = format_graph_shape_text(compare_graph_shapes(*same))
    assert "B: skipped 2 malformed line(s) in trace.jsonl" in text
    assert "the shape covers only the records that parsed" in text


# ── the CLI ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["text", "json", "github-annotation"])
def test_gate_exits_2_when_the_parsed_shapes_match(
    same: tuple[Path, Path], fmt: str
) -> None:
    a, b = same
    result = runner.invoke(
        app, ["diff", str(a), str(b), "--assert-same-shape", "--output-format", fmt]
    )
    assert result.exit_code == 2, result.output
    err = strip_ansi(result.stderr)
    assert "--assert-same-shape: cannot compare" in err
    assert "trace.jsonl" in err


def test_gate_exits_2_not_1_when_the_parsed_shapes_differ(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", "RUNA", ("web_search",))
    b = _capsule(tmp_path, "b", "RUNB", ("web_search", "git"))
    _corrupt_trace(b)
    result = runner.invoke(app, ["diff", str(a), str(b), "--assert-same-shape"])
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize("fmt", ["text", "json", "github-annotation"])
def test_without_the_gate_it_warns_on_stderr_and_exits_0(
    same: tuple[Path, Path], fmt: str
) -> None:
    a, b = same
    result = runner.invoke(app, ["diff", str(a), str(b), "--graph-shape", "--output-format", fmt])
    assert result.exit_code == 0, result.output
    err = strip_ansi(result.stderr)
    assert "warning: graph shape: skipped 2 malformed line(s) in trace.jsonl of run B" in err
    if fmt == "json":
        doc = json.loads(result.stdout)
        assert doc["graph_shape"]["b"]["skipped_malformed_lines"]["trace"] == 2
    if fmt == "github-annotation":
        assert "::warning title=NovaFabric Graph Shape::Skipped 2 malformed line(s)" in (
            result.stdout
        )


def test_clean_capsules_gate_unchanged(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", "RUNA", ("web_search",))
    b = _capsule(tmp_path, "b", "RUNB", ("web_search",))
    c = _capsule(tmp_path, "c", "RUNC", ("web_search", "git"))
    assert runner.invoke(app, ["diff", str(a), str(b), "--assert-same-shape"]).exit_code == 0
    result = runner.invoke(app, ["diff", str(a), str(c), "--assert-same-shape"])
    assert result.exit_code == 1
    assert "warning" not in strip_ansi(result.stderr)


def test_help_documents_malformed_graph_sources_as_cannot_compare() -> None:
    result = runner.invoke(app, ["diff", "--help"], terminal_width=200)
    text = " ".join(strip_ansi(result.output).split())
    assert "--assert-same-shape could not build a graph or read malformed graph-source lines" in text
