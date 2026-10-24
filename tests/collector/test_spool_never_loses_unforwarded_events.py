"""The pure-Python spool never drops or overwrites an event nobody forwarded.

Review capture-runners-16: ``_evict_if_needed`` unlinked the oldest committed
segments past ``max_files`` (100) with no log or counter — a ``--emit-spool`` run
with more than ~100 calls deleted its own ``RunStarted`` before any forwarder drained
it. The next sequence number was scanned once at start-up and segments were
committed with ``rename(2)``, so two processes on the same spool replaced each
other's segments. Now (as the Go writer, review go-collector-ts-packages-03): only
segments the forwarder's ``checkpoint.json`` acknowledges are evicted, a full spool
refuses the write with :class:`SpoolFullError`, sequence numbers start above every
committed segment and the checkpoint, and a segment is committed with ``link(2)``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from novafabric.collector_cffi.spool import SpoolFullError, _PurePythonSpool

HEADER = 16


def _segments(spool: Path) -> list[Path]:
    return sorted(spool.glob("*.jsonl"))


def _payload(path: Path) -> bytes:
    return path.read_bytes()[HEADER:]


def _ack(spool: Path, seq: int) -> None:
    (spool / "checkpoint.json").write_text(json.dumps(
        {"last_ack_seq": seq, "last_ack_offset": 0, "initialized": True}
    ))


def test_more_events_than_the_old_cap_are_all_kept(tmp_path: Path) -> None:
    spool = _PurePythonSpool(tmp_path)
    for i in range(152):
        spool.write(json.dumps({"i": i}).encode())
    segs = _segments(tmp_path)
    assert len(segs) == 152
    assert json.loads(_payload(segs[0])) == {"i": 0}  # the run's first event survives


def test_only_acknowledged_segments_are_evicted(tmp_path: Path) -> None:
    spool = _PurePythonSpool(tmp_path, max_files=3)
    for i in range(3):
        spool.write(json.dumps({"i": i}).encode())
    _ack(tmp_path, 0)  # the forwarder has acknowledged seq 0 only
    spool.write(b'{"i": 3}')
    names = [p.name for p in _segments(tmp_path)]
    assert names == [f"{n:020d}.jsonl" for n in (1, 2, 3)]


def test_a_full_spool_refuses_rather_than_evicting_unforwarded_events(tmp_path: Path) -> None:
    spool = _PurePythonSpool(tmp_path, max_files=2)
    spool.write(b'{"i": 0}')
    spool.write(b'{"i": 1}')
    with pytest.raises(SpoolFullError):
        spool.write(b'{"i": 2}')
    assert len(_segments(tmp_path)) == 2


def test_a_new_writer_starts_above_the_checkpoint(tmp_path: Path) -> None:
    _ack(tmp_path, 41)  # acknowledged and evicted: the directory is empty
    _PurePythonSpool(tmp_path).write(b'{"after": "ack"}')
    assert [p.name for p in _segments(tmp_path)] == [f"{42:020d}.jsonl"]


def test_two_writers_on_one_spool_never_overwrite_each_other(tmp_path: Path) -> None:
    a, b = _PurePythonSpool(tmp_path), _PurePythonSpool(tmp_path)
    a.write(b'{"w": "a0"}')
    b.write(b'{"w": "b0"}')
    a.write(b'{"w": "a1"}')
    payloads = sorted(_payload(p) for p in _segments(tmp_path))
    assert payloads == sorted([b'{"w": "a0"}\n', b'{"w": "b0"}\n', b'{"w": "a1"}\n'])


def test_a_commit_collision_moves_to_the_next_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process committed this sequence between our scan and our link."""
    spool = _PurePythonSpool(tmp_path)
    real_link = os.link
    planted: list[str] = []

    def link(src: str, dst: str) -> None:
        if not planted:
            planted.append(dst)
            Path(dst).write_bytes(b"x" * HEADER + b'{"other": "process"}\n')
        real_link(src, dst)

    monkeypatch.setattr(os, "link", link)
    spool.write(b'{"mine": 1}')
    payloads = {p.name: _payload(p) for p in _segments(tmp_path)}
    assert payloads[Path(planted[0]).name] == b'{"other": "process"}\n'
    assert b'{"mine": 1}\n' in payloads.values()
    # The header carries the sequence the segment was finally committed under.
    mine = next(p for p in _segments(tmp_path) if _payload(p) == b'{"mine": 1}\n')
    assert int.from_bytes(mine.read_bytes()[4:12], "little") == int(mine.stem)
    assert not list(tmp_path.glob("*.tmp"))


# ------------------------------------------------------------ the capture sink


def test_the_sink_counts_a_refused_write(tmp_path: Path) -> None:
    from unittest.mock import patch

    from novafabric.capture.spool_sink import SpoolSink

    sink = SpoolSink(tmp_path)
    with patch.object(sink._spool, "write", side_effect=SpoolFullError("full")), patch(
        "novafabric.capture.spool_sink.note_capture_failure"
    ) as noted:
        sink.emit_event(event_type="RunStarted", run_id="r", agent_id="a")  # never raises
    (call,) = noted.call_args_list
    assert call.args[:2] == ("spool-sink", "spool")
    assert sink.dropped == 1


def test_a_non_object_tool_call_line_does_not_abort_re_emission(tmp_path: Path) -> None:
    from novafabric.capture.spool_sink import SpoolSink, emit_call_events_from_capsule

    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "tool-calls.jsonl").write_text(
        '[1]\n"text"\n{"tool_name": "search", "status": "success"}\n'
    )
    spool = tmp_path / "spool"
    emit_call_events_from_capsule(SpoolSink(spool), cap, run_id="r", agent_id="a")
    (seg,) = _segments(spool)
    assert json.loads(_payload(seg))["event_type"] == "ToolCallCompleted"
