"""EventRecorder fail-open observability (v0.61 audit, Wave 1).

The recorder's fail-open contract (never block the workload) stays intact,
but swallowed failures must stop being invisible: each dropped event is
counted per stream, the first drop per stream logs one rate-limited
warning, and `finalize_health()` writes a `capture-health.json` block into
the capsule — only when something was actually dropped, so clean captures
are byte-identical to before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from novafabric.capture.event_recorder import EventRecorder


def _recorder(capsule_dir: Path) -> EventRecorder:
    return EventRecorder(capsule_dir=capsule_dir, run_id="r-1", capsule_id="r-1")


def _broken_dir(tmp_path: Path) -> Path:
    """A 'directory' path that is actually a file — every append fails."""
    p = tmp_path / "not-a-dir"
    p.write_text("occupied")
    return p


def test_successful_records_leave_no_drops(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.record_file_event(operation="read", path="/x")
    assert rec.drop_counts == {}


def test_failed_append_is_counted_not_raised(tmp_path: Path) -> None:
    rec = _recorder(_broken_dir(tmp_path))
    rec.record_file_event(operation="read", path="/x")  # must not raise
    rec.record_file_event(operation="write", path="/y")
    assert rec.drop_counts == {"file_events.jsonl": 2}


def test_first_drop_per_stream_logs_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rec = _recorder(_broken_dir(tmp_path))
    with caplog.at_level(logging.WARNING, logger="novafabric.capture.event_recorder"):
        rec.record_file_event(operation="read", path="/a")
        rec.record_file_event(operation="read", path="/b")
        rec.record_file_event(operation="read", path="/c")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "file_events.jsonl" in warnings[0].getMessage()


def test_finalize_health_writes_drop_report(tmp_path: Path) -> None:
    broken = _broken_dir(tmp_path)
    rec = _recorder(broken)
    rec.record_file_event(operation="read", path="/x")
    # Health report goes to a WRITABLE dir (the real capsule dir at run end).
    out_dir = tmp_path / "capsule"
    out_dir.mkdir()
    rec.finalize_health(out_dir)
    report = json.loads((out_dir / "capture-health.json").read_text())
    assert report["run_id"] == "r-1"
    assert report["dropped_events"] == {"file_events.jsonl": 1}


def test_finalize_health_writes_nothing_when_clean(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.record_file_event(operation="read", path="/x")
    rec.finalize_health(tmp_path)
    assert not (tmp_path / "capture-health.json").exists()


def test_finalize_health_is_itself_fail_open(tmp_path: Path) -> None:
    rec = _recorder(_broken_dir(tmp_path))
    rec.record_file_event(operation="read", path="/x")
    # Writing the report to an unwritable target must not raise either.
    rec.finalize_health(_broken_dir(tmp_path))
