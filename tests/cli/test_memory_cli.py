# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""`nova memory lineage|trace` — ADR-0143 P1 CLI surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _write_events(capsule: Path, records: list[dict[str, object]]) -> None:
    lines = [json.dumps({"event_type": "MemoryOperation", **r}) for r in records]
    (capsule / "memory_operations.jsonl").write_text("\n".join(lines) + "\n")


def _rec(
    operation: str, run_id: str, key: str = "user_prefs", ts: str = "2026-07-20T10:00:00Z"
) -> dict[str, object]:
    return {
        "event_id": f"{run_id}-{operation}-{ts}",
        "run_id": run_id,
        "capsule_id": "01J0000000000000000000CAPS",
        "timestamp_utc": ts,
        "operation": operation,
        "memory_key": key,
    }


@pytest.fixture
def capsule(tmp_path: Path) -> Path:
    d = tmp_path / "capsule"
    d.mkdir()
    return d


# ── lineage ───────────────────────────────────────────────────────────────


def test_lineage_lists_edges(capsule: Path) -> None:
    _write_events(capsule, [_rec("write", "run-a"), _rec("read", "run-b")])
    res = runner.invoke(app, ["memory", "lineage", str(capsule), "-o", "json"])
    assert res.exit_code == 0
    edges = json.loads(res.stdout)
    assert {e["edge_type"] for e in edges} == {"wrote_memory", "read_memory"}


def test_lineage_on_capsule_without_memory_facet_is_not_an_error(
    capsule: Path,
) -> None:
    """A capsule is valid without the facet (ADR-0143 P1)."""
    res = runner.invoke(app, ["memory", "lineage", str(capsule)])
    assert res.exit_code == 0
    assert "No memory operations" in res.stdout


def test_lineage_rejects_a_non_capsule_path(tmp_path: Path) -> None:
    res = runner.invoke(app, ["memory", "lineage", str(tmp_path / "nope")])
    assert res.exit_code == 2


def test_malformed_lines_are_skipped_with_a_warning(capsule: Path) -> None:
    """A crashed run leaves a truncated tail — precisely when a trace matters."""
    path = capsule / "memory_operations.jsonl"
    path.write_text(
        json.dumps({"event_type": "MemoryOperation", **_rec("write", "run-a")})
        + "\n{ truncated"
    )
    res = runner.invoke(app, ["memory", "lineage", str(capsule), "-o", "json"])
    assert res.exit_code == 0
    assert len(json.loads(res.stdout)) == 1


# ── trace ─────────────────────────────────────────────────────────────────


def test_trace_reports_writers_and_readers(capsule: Path) -> None:
    _write_events(
        capsule,
        [
            _rec("write", "run-good", ts="2026-07-20T10:00:00Z"),
            _rec("write", "run-poison", ts="2026-07-20T11:00:00Z"),
            _rec("read", "run-victim", ts="2026-07-20T12:00:00Z"),
        ],
    )
    res = runner.invoke(
        app, ["memory", "trace", str(capsule), "--key", "user_prefs", "-o", "json"]
    )
    assert res.exit_code == 0
    out = json.loads(res.stdout)
    assert out["writers"] == ["run-good", "run-poison"]
    assert out["readers"] == ["run-victim"]


def test_trace_of_an_unknown_key_is_empty_not_an_error(capsule: Path) -> None:
    _write_events(capsule, [_rec("write", "run-a")])
    res = runner.invoke(
        app, ["memory", "trace", str(capsule), "--key", "absent", "-o", "json"]
    )
    assert res.exit_code == 0
    assert json.loads(res.stdout) == {
        "memory_key": "absent",
        "writers": [],
        "readers": [],
    }


def test_no_namespace_flag_is_offered() -> None:
    """A namespace flag would be applied to both build and query, cancelling out.

    Rather than ship a flag that cannot affect the answer, the CLI omits it.
    """
    res = runner.invoke(app, ["memory", "trace", "--help"])
    assert "--namespace" not in res.stdout


# ── Round-trip against the real recorder ──────────────────────────────────


def test_recorder_output_is_readable_by_the_cli(tmp_path: Path) -> None:
    """Guards the seam: the recorder's on-disk shape must parse here.

    Both halves were written together; testing each against my own fixture
    would prove only that they match my assumption, not each other.
    """
    from novafabric.capture.event_recorder import EventRecorder

    rec = EventRecorder(
        capsule_dir=tmp_path,
        run_id="01J0000000000000000000RUNA",
        capsule_id="01J0000000000000000000CAPS",
    )
    rec.record_memory_operation(operation="write", memory_key="k")
    rec.record_memory_operation(
        operation="read", memory_key="k", origin_run_id="01J0000000000000000000RUNA"
    )

    res = runner.invoke(
        app, ["memory", "trace", str(tmp_path), "--key", "k", "-o", "json"]
    )
    assert res.exit_code == 0, res.stdout
    out = json.loads(res.stdout)
    assert out["writers"] == ["01J0000000000000000000RUNA"]
    assert out["readers"] == ["01J0000000000000000000RUNA"]
