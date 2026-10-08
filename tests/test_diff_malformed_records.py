"""Malformed capsule record lines are counted and surfaced, never skipped silently.

``nova diff`` dropped a record line it could not parse without a word, so a
corrupted record vanished from both sides of the comparison and
``--assert-no-regressions`` passed (exit 0) on runs it had not fully read. A
valid-JSON line that is not an object (``[1, 2]``) crashed the aligner instead,
exiting 1 — the "found a difference" code. ADR-0303 Amendment 1: the lines are
counted per side and per record file, warned about on stderr, reported in every
output format, and make the gate exit 2, "cannot compare".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.diff._engine import DiffEngine, _read_jsonl

runner = CliRunner()


def _call(prompt: str) -> dict[str, Any]:
    return {
        "model_call_id": prompt,
        "parent_span_id": "root",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.request.messages": [{"role": "user", "content": prompt}],
    }


def _capsule(
    root: Path, run_id: str, model_lines: list[str], tool_lines: tuple[str, ...] = ()
) -> Path:
    d = root / run_id
    (d / "outputs").mkdir(parents=True)
    (d / "capsule.yaml").write_text(f"run_id: {run_id}\nstatus: success\n")
    (d / "env.lock").write_text("python: '3.12'\n")
    (d / "model-calls.jsonl").write_text("".join(line + "\n" for line in model_lines))
    (d / "tool-calls.jsonl").write_text("".join(line + "\n" for line in tool_lines))
    return d


GOOD = json.dumps(_call("x"))


@pytest.fixture
def corrupt_b(tmp_path: Path) -> tuple[Path, Path]:
    """B holds A's record plus two unreadable lines: the parsed records match."""
    a = _capsule(tmp_path, "run-a", [GOOD])
    b = _capsule(tmp_path, "run-b", [GOOD, "{not json", "[1, 2]"])
    return a, b


# ── the reader ───────────────────────────────────────────────────────────────


def test_reader_counts_every_kind_of_malformed_line(tmp_path: Path) -> None:
    path = tmp_path / "model-calls.jsonl"
    path.write_bytes(
        GOOD.encode() + b"\n"
        + b"{not json\n"
        + b"[1, 2]\n"          # valid JSON, not an object
        + b'"a string"\n'      # valid JSON, not an object
        + b"\xff\xfe{}\n"      # not UTF-8
        + b"   \n\n"           # blank lines are not records and not malformed
    )
    records, skipped = _read_jsonl(path)
    assert [r["model_call_id"] for r in records] == ["x"]
    assert skipped == 4


def test_reader_keeps_a_record_with_an_unescaped_line_separator(tmp_path: Path) -> None:
    """U+2028 is legal unescaped inside a JSON string; str.splitlines cut there."""
    path = tmp_path / "model-calls.jsonl"
    path.write_text(json.dumps({"k": "a b"}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert _read_jsonl(path) == ([{"k": "a b"}], 0)


def test_missing_file_is_empty_not_malformed(tmp_path: Path) -> None:
    assert _read_jsonl(tmp_path / "absent.jsonl") == ([], 0)


# ── the report ───────────────────────────────────────────────────────────────


def test_report_counts_per_side_and_per_record_file(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "run-a", [GOOD, "{bad"], ("nope",))
    b = _capsule(tmp_path, "run-b", [GOOD], ("[]", "{", "1"))
    report = DiffEngine().compare(a, b)
    assert report.skipped_malformed_lines == {
        "a": {"model_calls": 1, "tool_calls": 1},
        "b": {"model_calls": 0, "tool_calls": 3},
    }
    assert report.malformed_line_count == 5
    assert report.is_complete is False
    assert report.has_changes is False  # the parsed records match


def test_a_clean_pair_is_complete_and_reports_zeros(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "run-a", [GOOD])
    b = _capsule(tmp_path, "run-b", [GOOD])
    report = DiffEngine().compare(a, b)
    assert report.is_complete
    assert report.as_dict()["skipped_malformed_lines"] == {
        "a": {"model_calls": 0, "tool_calls": 0},
        "b": {"model_calls": 0, "tool_calls": 0},
    }


# ── the CLI: never silent, in any output format ──────────────────────────────


@pytest.mark.parametrize("fmt", ["text", "json", "github-annotation"])
def test_warning_on_stderr_in_every_format(corrupt_b: tuple[Path, Path], fmt: str) -> None:
    a, b = corrupt_b
    result = runner.invoke(app, ["diff", str(a), str(b), "--output-format", fmt])
    assert result.exit_code == 0, result.output  # no gate flag: the diff only reports
    assert "warning: skipped 2 malformed line(s) in model-calls.jsonl of run B" in result.stderr
    assert "warning: skipped" not in result.stdout  # stdout stays the report alone


def test_json_carries_the_counts_and_stays_parseable(corrupt_b: tuple[Path, Path]) -> None:
    a, b = corrupt_b
    result = runner.invoke(app, ["diff", str(a), str(b), "--output-format", "json"])
    doc = json.loads(result.stdout)  # the stderr warning did not corrupt stdout
    assert doc["skipped_malformed_lines"] == {
        "a": {"model_calls": 0, "tool_calls": 0},
        "b": {"model_calls": 2, "tool_calls": 0},
    }
    assert doc["has_changes"] is False


def test_text_does_not_claim_no_differences_unqualified(corrupt_b: tuple[Path, Path]) -> None:
    a, b = corrupt_b
    out = " ".join(runner.invoke(app, ["diff", str(a), str(b)]).stdout.split())
    assert "Skipped (not compared):" in out
    assert "No differences found in the records that parsed; the comparison is incomplete." in out


def test_annotations_warn_about_skipped_lines(corrupt_b: tuple[Path, Path]) -> None:
    a, b = corrupt_b
    out = runner.invoke(
        app, ["diff", str(a), str(b), "--output-format", "github-annotation"]
    ).stdout.splitlines()
    assert any(
        line.startswith("::warning title=NovaFabric Diff::Skipped 2 malformed line(s)")
        for line in out
    ), out
    assert "::notice title=NovaFabric Diff::No differences found." not in out


# ── the gate: exit 2, "cannot compare" (ADR-0303 Amendment 1) ────────────────


def test_gate_exits_2_when_the_parsed_records_match(corrupt_b: tuple[Path, Path]) -> None:
    """It exited 0: the gate passed on a capsule it had not fully read."""
    a, b = corrupt_b
    result = runner.invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert result.exit_code == 2, result.output
    assert "cannot compare" in result.stderr


def test_gate_exits_2_not_1_even_when_the_parsed_records_differ(tmp_path: Path) -> None:
    """A skipped line may be the record an "added" entry pairs with: 1 is not established."""
    a = _capsule(tmp_path, "run-a", ["{truncated"])
    b = _capsule(tmp_path, "run-b", [GOOD])
    result = runner.invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert result.exit_code == 2, result.output


def test_a_non_object_line_no_longer_crashes_with_the_difference_code(tmp_path: Path) -> None:
    """``[1, 2]`` raised AttributeError in the aligner, which exited 1."""
    a = _capsule(tmp_path, "run-a", [GOOD])
    b = _capsule(tmp_path, "run-b", [GOOD, "[1, 2]"])
    plain = runner.invoke(app, ["diff", str(a), str(b)])
    assert plain.exit_code == 0 and plain.exception is None, plain.output
    gated = runner.invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert gated.exit_code == 2


def test_clean_capsules_gate_unchanged(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "run-a", [GOOD])
    b = _capsule(tmp_path, "run-b", [GOOD])
    same = runner.invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert same.exit_code == 0 and same.stderr == ""
    c = _capsule(tmp_path, "run-c", [GOOD, json.dumps(_call("y"))])
    assert runner.invoke(app, ["diff", str(a), str(c), "--assert-no-regressions"]).exit_code == 1


@pytest.mark.parametrize(
    "schema_path",
    ["src/novafabric/schemas/diff-report.schema.json", "schemas/diff-report.schema.json"],
)
def test_json_with_skipped_lines_validates_against_both_schemas(
    corrupt_b: tuple[Path, Path], schema_path: str
) -> None:
    import jsonschema

    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / schema_path).read_text())
    assert "skipped_malformed_lines" in schema["properties"], "schema documents the field"
    a, b = corrupt_b
    doc = json.loads(runner.invoke(app, ["diff", str(a), str(b), "--output-format", "json"]).stdout)
    jsonschema.Draft202012Validator(schema).validate(doc)
    bad = dict(doc, skipped_malformed_lines={"a": {"model_calls": -1}})
    assert not jsonschema.Draft202012Validator(schema).is_valid(bad)


def test_help_documents_malformed_lines_as_cannot_compare() -> None:
    from _help_assert import strip_ansi

    result = runner.invoke(app, ["diff", "--help"], terminal_width=200)
    out = " ".join(strip_ansi(result.output).split())
    assert "--assert-no-regressions read a capsule with malformed record lines" in out
