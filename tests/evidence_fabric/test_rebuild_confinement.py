"""``nova collector rebuild``: a broker-supplied ``run_id`` never names a file outside the target.

Review trust-evidence-07 (reproduced): each event's ``run_id`` became
``open(target_dir / f"{run_id}.jsonl", "ab")``. Whoever could publish to the
broker appended JSON to any ``*.jsonl`` the user could write (an absolute
``run_id`` wins the ``/`` join), such as the hash-chained audit log. Rebuilding
into a directory that already held a partition appended a second pass while
the reported digest described one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.collector import collector_app
from novafabric.evidence_fabric.rebuild import (
    OffsetReplayRebuilder,
    RebuildError,
    partition_file_name,
    rebuild_from_events,
)


def _event(run_id: str, seq: int = 0) -> bytes:
    return json.dumps({"run_id": run_id, "seq": seq, "injected": True}).encode()


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }


def test_hostile_run_ids_stay_inside_the_target(tmp_path: Path) -> None:
    victim = tmp_path / "elsewhere" / "audit.jsonl"
    victim.parent.mkdir()
    victim.write_bytes(b'{"legit": 1}\n')
    (tmp_path / "victim-dir").mkdir()
    target = tmp_path / "rebuilt"
    hostile = [
        str(victim.with_suffix("")),  # absolute: "<...>/elsewhere/audit" + ".jsonl"
        "../elsewhere/audit",
        "../../escape",
        "a/../../victim-dir/x",
        "..\\x",
        "x\x00y",
        "..",
        ".hidden",
    ]
    before = _tree(tmp_path)

    report = rebuild_from_events([_event(r) for r in hostile], target)

    assert victim.read_bytes() == b'{"legit": 1}\n'
    outside = {k: v for k, v in _tree(tmp_path).items() if not k.startswith("rebuilt/")}
    assert outside == before
    files = sorted(p.name for p in target.iterdir())
    assert len(files) == len(hostile)
    assert all(name.startswith("_unsafe-") and name.endswith(".jsonl") for name in files)
    # Each record keeps its original run_id inside it, so nothing is lost.
    by_run = {run.run_id: run for run in report.runs}
    for run_id in hostile:
        run = by_run[run_id]
        assert run.partition_file == partition_file_name(run_id)
        line = (target / run.partition_file).read_bytes()
        assert json.loads(line)["run_id"] == run_id
        assert hashlib.sha256(line).hexdigest() == run.sha256


def test_clean_run_ids_keep_their_file_names(tmp_path: Path) -> None:
    report = rebuild_from_events([_event("run-a"), _event("01HX0000000000000000000000")], tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "01HX0000000000000000000000.jsonl",
        "run-a.jsonl",
    ]
    assert {r.partition_file for r in report.runs} == {
        "01HX0000000000000000000000.jsonl",
        "run-a.jsonl",
    }


def test_a_link_at_a_partition_name_is_never_written_through(tmp_path: Path) -> None:
    victim = tmp_path / "victim.jsonl"
    victim.write_bytes(b"precious\n")
    target = tmp_path / "rebuilt"
    target.mkdir()
    (target / "run-a.jsonl").symlink_to(victim)
    with pytest.raises(RebuildError, match="already exists"):
        rebuild_from_events([_event("run-a")], target)
    assert victim.read_bytes() == b"precious\n"


def test_rebuilding_into_a_used_directory_is_refused_not_appended(tmp_path: Path) -> None:
    rebuild_from_events([_event("run-a", 0)], tmp_path)
    first = (tmp_path / "run-a.jsonl").read_bytes()
    with pytest.raises(RebuildError, match="empty directory"):
        rebuild_from_events([_event("run-a", 1)], tmp_path)
    assert (tmp_path / "run-a.jsonl").read_bytes() == first


def test_the_cli_refuses_a_used_target_before_draining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drained: list[bool] = []

    async def fake_drain(self: OffsetReplayRebuilder) -> list[bytes]:
        drained.append(True)
        return [_event("run-a")]

    monkeypatch.setattr(OffsetReplayRebuilder, "drain", fake_drain)
    target = tmp_path / "rebuilt"
    target.mkdir()
    (target / "run-a.jsonl").write_bytes(b"old\n")
    result = CliRunner().invoke(collector_app, ["rebuild", "--target", str(target)])
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "empty" in result.output and "already holds" in result.output
    assert drained == []
    assert (target / "run-a.jsonl").read_bytes() == b"old\n"


def test_rebuild_method_refuses_a_used_target(tmp_path: Path) -> None:
    (tmp_path / "x.jsonl").write_bytes(b"")
    with pytest.raises(RebuildError):
        asyncio.run(OffsetReplayRebuilder().rebuild(tmp_path))
