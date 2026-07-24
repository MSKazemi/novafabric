"""ADR-0118 P2: auto-populate a queue from the capsule store.

The `SubjectSelector` modelled run_ids/tool_names/tags/sample from the first
slice, but was only ever enforced as an *enqueue-time guard* — nothing
enumerated stored capsules against it, so every subject had to be added by
hand. This closes that.

Two properties matter most and are pinned here: population is **idempotent**
(safe to schedule) and sampling is **deterministic** (an auditor asking why a
run was reviewed gets a stable answer, not "chance").
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from novafabric.eval.annotation_queue import AnnotationError, SubjectSelector
from novafabric.eval.annotation_store import (
    create_queue,
    list_items,
    populate_queue,
)
from novafabric.eval.score_config import ScoreRange, ScoreValueType
from novafabric.eval.score_config_catalog import register_config


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "registry.db"
    register_config(
        "factuality",
        ScoreValueType.NUMERIC,
        "Higher is better.",
        range_=ScoreRange(min=0, max=1),
        db_path=path,
    )
    return path


def _capsule(root: Path, run_id: str, *, tags: list[str] | None = None,
             tools: list[str] | None = None) -> Path:
    cap = root / run_id
    cap.mkdir(parents=True)
    manifest: dict = {"run_id": run_id}
    if tags:
        manifest["metadata"] = {"tags": tags}
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    (cap / "trace.jsonl").write_text('{"event": "start"}\n')
    if tools:
        (cap / "tool-calls.jsonl").write_text(
            "\n".join('{"tool_name": "%s"}' % t for t in tools) + "\n"
        )
    return cap


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    root = tmp_path / "capsules"
    root.mkdir()
    _capsule(root, "01HXAY7M5JZ8R7K4P9DPBYK2WX", tags=["prod"], tools=["search"])
    _capsule(root, "01HXAY7M5JZ8R7K4P9DPBYK2WY", tags=["dev"], tools=["db.query"])
    _capsule(root, "01HXAY7M5JZ8R7K4P9DPBYK2WZ", tags=["prod"], tools=["search", "email"])
    return root


def _queue(db: Path, selector: SubjectSelector | None = None, name: str = "q1"):
    return create_queue(
        name=name, criteria=["factuality"], subject_selector=selector, db_path=db
    )


def test_empty_selector_enqueues_every_capsule(db: Path, store: Path) -> None:
    _queue(db)
    summary = populate_queue("q1", store, db_path=db)
    assert summary["scanned"] == 3
    assert summary["added"] == 3
    assert len(list_items("q1", db_path=db)) == 3


def test_tag_selector_filters(db: Path, store: Path) -> None:
    _queue(db, SubjectSelector(tags=["prod"]))
    summary = populate_queue("q1", store, db_path=db)
    assert summary["matched"] == 2
    assert summary["added"] == 2


def test_tool_name_selector_filters(db: Path, store: Path) -> None:
    _queue(db, SubjectSelector(tool_names=["email"]))
    summary = populate_queue("q1", store, db_path=db)
    assert summary["added"] == 1


def test_run_ids_selector_filters(db: Path, store: Path) -> None:
    _queue(db, SubjectSelector(run_ids=["01HXAY7M5JZ8R7K4P9DPBYK2WY"]))
    assert populate_queue("q1", store, db_path=db)["added"] == 1


def test_selector_keys_are_anded(db: Path, store: Path) -> None:
    """All present keys must hold, not any of them."""
    _queue(db, SubjectSelector(tags=["prod"], tool_names=["db.query"]))
    # 'prod' capsules use search/email; the db.query one is tagged 'dev'.
    assert populate_queue("q1", store, db_path=db)["added"] == 0


def test_population_is_idempotent(db: Path, store: Path) -> None:
    """Safe to schedule: a second run adds nothing."""
    _queue(db)
    first = populate_queue("q1", store, db_path=db)
    second = populate_queue("q1", store, db_path=db)
    assert first["added"] == 3
    assert second["added"] == 0
    assert second["skipped_existing"] == 3
    assert len(list_items("q1", db_path=db)) == 3


def test_new_capsules_are_picked_up_on_a_later_run(db: Path, store: Path) -> None:
    _queue(db)
    populate_queue("q1", store, db_path=db)
    _capsule(store, "01HXAY7M5JZ8R7K4P9DPBYK2XA", tags=["prod"])
    assert populate_queue("q1", store, db_path=db)["added"] == 1


def test_sampling_is_deterministic(db: Path, store: Path) -> None:
    """The same store must yield the same sample every time.

    A random sample would make the review set unreproducible — for an
    evidence product that is a defect, not a convenience.
    """
    _queue(db, SubjectSelector(sample=0.5), name="qa")
    _queue(db, SubjectSelector(sample=0.5), name="qb")
    a = populate_queue("qa", store, db_path=db)
    b = populate_queue("qb", store, db_path=db)
    assert a["added"] == b["added"]
    assert {i.subject for i in list_items("qa", db_path=db)} == {
        i.subject for i in list_items("qb", db_path=db)
    }


def test_sample_of_one_keeps_everything(db: Path, store: Path) -> None:
    _queue(db, SubjectSelector(sample=1.0))
    assert populate_queue("q1", store, db_path=db)["added"] == 3


def test_dry_run_writes_nothing(db: Path, store: Path) -> None:
    _queue(db)
    summary = populate_queue("q1", store, db_path=db, dry_run=True)
    assert summary["added"] == 3
    assert summary["dry_run"] is True
    assert list_items("q1", db_path=db) == []


def test_span_scoped_queue_refuses_auto_population(db: Path, store: Path) -> None:
    """Spans are not enumerable from the capsule store — say so, don't guess."""
    _queue(db, SubjectSelector(subject_kind="span"))
    with pytest.raises(AnnotationError, match="span"):
        populate_queue("q1", store, db_path=db)


def test_malformed_tool_calls_do_not_abort_population(db: Path, tmp_path: Path) -> None:
    """One bad line must not cost the whole sweep."""
    root = tmp_path / "caps"
    root.mkdir()
    cap = _capsule(root, "01HXAY7M5JZ8R7K4P9DPBYK2WX", tags=["prod"])
    (cap / "tool-calls.jsonl").write_text('{"tool_name": "ok"}\nNOT JSON\n')
    _queue(db)
    assert populate_queue("q1", root, db_path=db)["added"] == 1


def test_missing_capsule_root_is_not_an_error(db: Path, tmp_path: Path) -> None:
    _queue(db)
    summary = populate_queue("q1", tmp_path / "nope", db_path=db)
    assert summary["scanned"] == 0 and summary["added"] == 0
