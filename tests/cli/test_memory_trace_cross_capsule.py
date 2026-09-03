"""`nova memory trace` must be able to see the blast radius (ADR-0143 NF-114).

The command's own docstring calls `readers` "the blast radius". A persistent
memory store exists so one run can read what another wrote, so a query scoped to
a single capsule cannot answer that: the runs that read a poisoned key live in
*their* capsules, not the writer's. It answered a different question from the one
it named.

The widening creates a new way to be wrong — a blast radius over 3 of 50 capsules
is not the blast radius either, and now looks authoritative. So coverage travels
with the answer, and that is tested in both output formats.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

KEY = "user.preferences"


def _capsule(tmp_path: Path, name: str, events: list[dict]) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "memory_operations.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + ("\n" if events else "")
    )
    return d


def _write(run: str, at: str) -> dict:
    return {"run_id": run, "capsule_id": run, "timestamp_utc": at,
            "operation": "write", "memory_key": KEY}


def _read(run: str, at: str, origin: str) -> dict:
    return {"run_id": run, "capsule_id": run, "timestamp_utc": at,
            "operation": "read", "memory_key": KEY, "origin_run_id": origin}


def _trace(capsule: str, key: str = KEY, *extra: str):
    """`nova memory trace CAPSULE --key KEY` — the key is an option, not positional."""
    return runner.invoke(app, ["memory", "trace", capsule, "--key", key, *extra])


# ── the gap this closes ──────────────────────────────────────────────────────


def test_a_reader_in_another_capsule_is_found(tmp_path: Path) -> None:
    """The whole point: run A wrote it, run B read it, and they are separate capsules."""
    a = _capsule(tmp_path, "a", [_write("run-a", "2026-07-01T00:00:00Z")])
    b = _capsule(tmp_path, "b", [_read("run-b", "2026-07-02T00:00:00Z", "run-a")])

    single = _trace(str(a), KEY, "-o", "json")
    both = _trace(str(a), KEY, "--also-capsule", str(b), "-o", "json")

    assert "run-b" not in json.loads(single.stdout)["readers"], (
        "a single-capsule query cannot see the reader — this is the defect"
    )
    assert json.loads(both.stdout)["readers"] == ["run-b"]
    assert json.loads(both.stdout)["writers"] == ["run-a"]


def test_writers_stay_ordered_across_capsules(tmp_path: Path) -> None:
    """`writers_of` returns the most recent writer last; that must hold across files."""
    late = _capsule(tmp_path, "late", [_write("run-late", "2026-07-09T00:00:00Z")])
    early = _capsule(tmp_path, "early", [_write("run-early", "2026-07-01T00:00:00Z")])

    result = _trace(str(late), KEY, "--also-capsule", str(early), "-o", "json")

    assert json.loads(result.stdout)["writers"] == ["run-early", "run-late"], (
        "ordering is by event time, not by the order capsules were supplied"
    )


def test_a_run_seen_twice_is_not_double_counted(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [_write("run-a", "2026-07-01T00:00:00Z")])

    result = _trace(str(a), KEY, "--also-capsule", str(a), "-o", "json")

    assert json.loads(result.stdout)["writers"] == ["run-a"]


# ── coverage travels with the answer ─────────────────────────────────────────


def test_json_reports_how_many_capsules_were_searched(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [_write("run-a", "2026-07-01T00:00:00Z")])
    empty = _capsule(tmp_path, "empty", [])

    payload = json.loads(
        _trace(str(a), KEY, "--also-capsule", str(empty), "-o", "json").stdout
    )

    assert payload["capsules_searched"] == 2
    assert payload["capsules_with_memory_operations"] == 1


def test_text_output_reports_coverage(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [_write("run-a", "2026-07-01T00:00:00Z")])
    result = _trace(str(a), KEY)

    assert "searched 1 capsule" in result.output


def test_an_empty_result_says_how_many_were_searched(tmp_path: Path) -> None:
    """Absence must not be ambiguous — 'nothing found' where?"""
    a = _capsule(tmp_path, "a", [])
    b = _capsule(tmp_path, "b", [])

    result = _trace(str(a), KEY, "--also-capsule", str(b))

    assert result.exit_code == 0, result.output
    assert "2 capsule(s) searched" in result.output


# ── failure must be loud ─────────────────────────────────────────────────────


def test_a_missing_extra_capsule_fails_loudly(tmp_path: Path) -> None:
    """Silently skipping one shrinks a blast radius without saying so."""
    a = _capsule(tmp_path, "a", [_write("run-a", "2026-07-01T00:00:00Z")])

    result = _trace(str(a), KEY, "--also-capsule", str(tmp_path / "nope"))

    assert result.exit_code == 2, result.output


def test_a_missing_primary_capsule_still_fails(tmp_path: Path) -> None:
    result = _trace(str(tmp_path / "nope"), KEY)
    assert result.exit_code == 2


# ── the existing form is unchanged ───────────────────────────────────────────


def test_the_single_capsule_positional_form_still_works(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [
        _write("run-a", "2026-07-01T00:00:00Z"),
        _read("run-a", "2026-07-01T00:01:00Z", "run-a"),
    ])

    result = _trace(str(a), KEY)

    assert result.exit_code == 0, result.output
    assert "run-a" in result.output


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_both_output_formats_still_work(fmt: str, tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [_write("run-a", "2026-07-01T00:00:00Z")])
    result = _trace(str(a), KEY, "-o", fmt)
    assert result.exit_code == 0, result.output
