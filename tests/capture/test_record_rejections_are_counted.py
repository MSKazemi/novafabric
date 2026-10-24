"""A ``record.*`` event its model rejects is counted as a drop, not lost silently.

Review capture-runners-06: every ``EventRecorder.record_*`` method wrapped the event
model's construction in ``except Exception: pass``. Only a failed *write* reached the
drop counter, so a pydantic ``ValidationError`` — ``operation="rename"``, a reranker
document without ``document_id`` — wrote nothing, logged nothing and left
``capture-health.json`` absent: the capsule read as complete.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

from novafabric.capture.event_recorder import EventRecorder

RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def _recorder(tmp_path: Path) -> EventRecorder:
    return EventRecorder(tmp_path, RUN_ID, RUN_ID)


def test_an_invalid_file_event_is_counted(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.record_file_event(operation="rename", path="x")  # not one of the Literal values
    assert rec.drop_counts == {"file_events.jsonl": 1}
    assert not (tmp_path / "file_events.jsonl").exists()


def test_an_invalid_reranker_document_is_counted(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.record_reranker("rr-1", documents=[{"score": 0.5}])  # no document_id
    assert rec.drop_counts == {"reranker_events.jsonl": 1}


def test_a_valid_event_is_written_and_not_counted(tmp_path: Path) -> None:
    rec = _recorder(tmp_path)
    rec.record_file_event(operation="write", path="x")
    assert rec.drop_counts == {}
    assert (tmp_path / "file_events.jsonl").is_file()


def test_every_record_method_counts_what_it_could_not_build() -> None:
    """Structural: no ``record_*`` method swallows an exception without a drop."""
    silent: list[str] = []
    for name, fn in inspect.getmembers(EventRecorder, inspect.isfunction):
        # record_tampered only buffers entries in memory for finalize_health; it
        # builds no event model and writes no stream.
        if not name.startswith("record_") or name == "record_tampered":
            continue
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
            calls = {
                c.func.attr for c in ast.walk(handler)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            }
            if "_note_drop" not in calls and not any(
                isinstance(n, ast.Raise) for n in ast.walk(handler)
            ):
                silent.append(f"{name}:{handler.lineno}")
    assert silent == []
