"""M14: drops in the workload's own process reach ``capture-health.json``.

Under ``nova capture`` the hooks run in the workload's child process (installed
by the runner's ``sitecustomize`` loader), so a fail-open drop there is counted
on the *child's* ``EventRecorder``. Before the fix nothing ever finalized that
recorder: the orchestrator wrote ``capture-health.json`` from its own recorder
only, and the child's loss vanished. Now the child writes its counts to a
small per-process file in the capsule at exit, and the orchestrator merges and
removes those files immediately before it writes ``capture-health.json``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from novafabric.capture.event_recorder import EventRecorder
from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.capture.workload_health import (
    WORKLOAD_HEALTH_PREFIX,
    flush_workload_drops,
    merge_workload_drops,
)

_DROPPING_CHILD = """\
from novafabric.capture.hooks._fail_open import note_capture_failure
note_capture_failure("test-hook", "model-calls.jsonl", RuntimeError("x"))
note_capture_failure("test-hook", "model-calls.jsonl", RuntimeError("x"))
note_capture_failure("test-hook", "tool-calls.jsonl", RuntimeError("x"))
print("workload ran")
"""


def _capture(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "workload.py"
    script.write_text(source)
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    assert result.exit_code == 0
    return result.capsule_dir


def _leftovers(capsule_dir: Path) -> list[str]:
    return [p.name for p in capsule_dir.iterdir() if p.name.startswith(WORKLOAD_HEALTH_PREFIX)]


# ── end to end, through the real runner and sitecustomize loader ────────────


def test_child_process_drops_reach_capture_health(tmp_path: Path) -> None:
    capsule = _capture(tmp_path, _DROPPING_CHILD)

    health_path = capsule / "capture-health.json"
    assert health_path.is_file(), "the workload's drops never reached the capsule"
    health = json.loads(health_path.read_text())
    assert health["dropped_events"] == {"model-calls.jsonl": 2, "tool-calls.jsonl": 1}
    assert health["sources"]["workload"] == {"model-calls.jsonl": 2, "tool-calls.jsonl": 1}
    # The per-process hand-off files are consumed, never left as evidence.
    assert _leftovers(capsule) == []
    # Written before the digest map, so it is bound like every other file.
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert "capture-health.json" in json.dumps(manifest["evidence_digests"])


def test_clean_child_writes_no_health_file(tmp_path: Path) -> None:
    capsule = _capture(tmp_path, "print('clean')\n")
    assert not (capsule / "capture-health.json").exists()
    assert _leftovers(capsule) == []


# ── the child side ──────────────────────────────────────────────────────────


def _recorder(capsule: Path) -> EventRecorder:
    return EventRecorder(capsule_dir=capsule, run_id="r-1", capsule_id="r-1")


def test_flush_writes_nothing_without_drops(tmp_path: Path) -> None:
    assert flush_workload_drops(tmp_path, _recorder(tmp_path)) is None
    assert flush_workload_drops(tmp_path, None) is None
    assert list(tmp_path.iterdir()) == []


def test_flush_writes_one_file_per_process(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.note_drop("network_events.jsonl")
    written = flush_workload_drops(tmp_path, rec)
    assert written is not None
    assert written.name == f"{WORKLOAD_HEALTH_PREFIX}{os.getpid()}.json"
    assert json.loads(written.read_text())["dropped_events"] == {"network_events.jsonl": 1}


def test_flush_never_writes_through_a_planted_link(tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("untouched")
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / f"{WORKLOAD_HEALTH_PREFIX}{os.getpid()}.json").symlink_to(target)
    rec = _recorder(capsule)
    rec.note_drop("model-calls.jsonl")
    assert flush_workload_drops(capsule, rec) is None  # fail-open, never raises
    assert target.read_text() == "untouched"


# ── the orchestrator side ───────────────────────────────────────────────────


def _hand_off(capsule: Path, pid: int, payload: object) -> Path:
    path = capsule / f"{WORKLOAD_HEALTH_PREFIX}{pid}.json"
    path.write_text(json.dumps(payload))
    return path


def test_merge_sums_every_process_and_removes_the_files(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.note_drop("model-calls.jsonl")  # the orchestrator's own drop
    _hand_off(tmp_path, 11, {"dropped_events": {"model-calls.jsonl": 2}})
    _hand_off(tmp_path, 12, {"dropped_events": {"tool-calls.jsonl": 3}})

    merged = merge_workload_drops(tmp_path, rec)

    assert merged == {"model-calls.jsonl": 2, "tool-calls.jsonl": 3}
    assert rec.drop_counts == {"model-calls.jsonl": 3, "tool-calls.jsonl": 3}
    assert _leftovers(tmp_path) == []
    rec.finalize_health()
    health = json.loads((tmp_path / "capture-health.json").read_text())
    assert health["dropped_events"] == {"model-calls.jsonl": 3, "tool-calls.jsonl": 3}
    assert health["sources"] == {
        "orchestrator": {"model-calls.jsonl": 1},
        "workload": {"model-calls.jsonl": 2, "tool-calls.jsonl": 3},
    }


def test_health_report_without_workload_drops_is_unchanged(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.note_drop("model-calls.jsonl")
    assert merge_workload_drops(tmp_path, rec) == {}
    rec.finalize_health()
    health = json.loads((tmp_path / "capture-health.json").read_text())
    assert "sources" not in health  # additive: absent unless the workload dropped


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        {"dropped_events": {"model-calls.jsonl": -1}},
        {"dropped_events": {"model-calls.jsonl": "2"}},
        {"dropped_events": {"model-calls.jsonl": True}},
        {"dropped_events": ["model-calls.jsonl"]},
        {"dropped_events": {"x" * 500: 1}},
        {"no_counts": 1},
    ],
)
def test_malformed_hand_off_is_ignored_and_removed(tmp_path: Path, payload: object) -> None:
    path = tmp_path / f"{WORKLOAD_HEALTH_PREFIX}7.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    rec = _recorder(tmp_path)
    assert merge_workload_drops(tmp_path, rec) == {}
    assert rec.drop_counts == {}
    assert not path.exists()


def test_merge_never_follows_a_planted_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"dropped_events": {"model-calls.jsonl": 99}}))
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    link = capsule / f"{WORKLOAD_HEALTH_PREFIX}8.json"
    link.symlink_to(outside)
    rec = _recorder(capsule)
    assert merge_workload_drops(capsule, rec) == {}
    assert not link.is_symlink() and not link.exists()  # the link is removed
    assert outside.exists()  # its target is not


def test_oversize_hand_off_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / f"{WORKLOAD_HEALTH_PREFIX}9.json"
    path.write_text(json.dumps({"dropped_events": {"a": 1}, "pad": "x" * 200_000}))
    rec = _recorder(tmp_path)
    assert merge_workload_drops(tmp_path, rec) == {}
    assert not path.exists()


def test_a_forked_child_never_writes_the_parents_counts(tmp_path: Path) -> None:
    """The atexit handler is inherited across fork(); only the registrant writes."""
    from novafabric.capture import workload_health

    rec = _recorder(tmp_path)
    rec.note_drop("model-calls.jsonl")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(workload_health, "get_process_recorder", lambda: rec)
        workload_health._flush_at_exit(tmp_path, os.getpid() + 1)
        assert _leftovers(tmp_path) == []
        workload_health._flush_at_exit(tmp_path, os.getpid())
        assert _leftovers(tmp_path) == [f"{WORKLOAD_HEALTH_PREFIX}{os.getpid()}.json"]
