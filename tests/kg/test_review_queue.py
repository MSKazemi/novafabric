"""Tests for HumanReviewQueueWriter (Tier-3 entity resolution queue, SQLite-backed).

All tests use a tmp_path fixture — no Postgres / asyncpg required.
"""
from __future__ import annotations

import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _queue(tmp_path: Path):
    from novafabric.kg.review_queue import HumanReviewQueueWriter

    return HumanReviewQueueWriter(db_path=tmp_path / "review_queue.db")


def _item(alias: str, entity_type: str = "model", confidence: float = 0.4, **kw):
    from novafabric.kg.review_queue import ReviewItem

    return ReviewItem(
        alias=alias,
        entity_type=entity_type,
        confidence=confidence,
        **kw,
    )


# ---------------------------------------------------------------------------
# enqueue() + list_pending()
# ---------------------------------------------------------------------------


def test_enqueue_and_list_pending(tmp_path: Path) -> None:
    """enqueue() writes an item; list_pending() returns it."""
    q = _queue(tmp_path)
    q.enqueue(_item("mystery-model"))
    items = q.list_pending()
    assert len(items) == 1
    assert items[0].alias == "mystery-model"
    assert items[0].status == "pending"
    q.close()


def test_list_pending_empty_initially(tmp_path: Path) -> None:
    """list_pending() returns [] on a fresh queue."""
    q = _queue(tmp_path)
    assert q.list_pending() == []
    q.close()


def test_enqueue_multiple_items(tmp_path: Path) -> None:
    """Multiple enqueues produce multiple pending items."""
    q = _queue(tmp_path)
    q.enqueue(_item("model-a"))
    q.enqueue(_item("model-b"))
    q.enqueue(_item("model-c"))
    items = q.list_pending()
    assert len(items) == 3
    aliases = {i.alias for i in items}
    assert aliases == {"model-a", "model-b", "model-c"}
    q.close()


def test_enqueue_preserves_context(tmp_path: Path) -> None:
    """Context dict is stored and retrieved correctly."""
    q = _queue(tmp_path)
    ctx = {"capsule_id": "cap-xyz", "run_id": "run-1"}
    q.enqueue(_item("ctx-model", context=ctx))
    items = q.list_pending()
    assert items[0].context == ctx
    q.close()


def test_duplicate_item_id_is_ignored(tmp_path: Path) -> None:
    """INSERT OR IGNORE: duplicate item_id does not create a second row."""
    from novafabric.kg.review_queue import ReviewItem

    q = _queue(tmp_path)
    fixed_id = "fixed-uuid-0001"
    q.enqueue(ReviewItem(item_id=fixed_id, alias="dup", entity_type="model"))
    q.enqueue(ReviewItem(item_id=fixed_id, alias="dup", entity_type="model"))
    items = q.list_pending()
    assert len(items) == 1
    q.close()


# ---------------------------------------------------------------------------
# approve()
# ---------------------------------------------------------------------------


def test_approve_changes_status(tmp_path: Path) -> None:
    """approve() sets status='approved' and records canonical + resolved_by."""
    q = _queue(tmp_path)
    item = _item("approve-me")
    q.enqueue(item)
    q.approve(item.item_id, canonical="gpt-4o", resolved_by="alice")

    # Approved items no longer appear in list_pending
    pending = q.list_pending()
    assert not any(i.item_id == item.item_id for i in pending)

    # Stats should reflect the approval
    s = q.stats()
    assert s["approved"] == 1
    assert s["pending"] == 0
    q.close()


def test_approve_sets_suggested_canonical(tmp_path: Path) -> None:
    """approve() writes canonical as suggested_canonical on the row."""

    q = _queue(tmp_path)
    item = _item("model-z")
    q.enqueue(item)
    q.approve(item.item_id, canonical="resolved-name", resolved_by="bob")

    row = q._get_conn().execute(
        "SELECT suggested_canonical FROM entity_review_queue WHERE item_id = ?",
        (item.item_id,),
    ).fetchone()
    assert row["suggested_canonical"] == "resolved-name"
    q.close()


def test_approve_only_affects_pending_items(tmp_path: Path) -> None:
    """approve() on a non-pending item_id is a no-op (status stays unchanged)."""
    q = _queue(tmp_path)
    # Approve a non-existent item — should not raise
    q.approve("nonexistent-id", canonical="x", resolved_by="test")
    assert q.stats()["approved"] == 0
    q.close()


# ---------------------------------------------------------------------------
# reject()
# ---------------------------------------------------------------------------


def test_reject_changes_status(tmp_path: Path) -> None:
    """reject() removes item from pending and increments rejected count."""
    q = _queue(tmp_path)
    item = _item("reject-me")
    q.enqueue(item)
    q.reject(item.item_id, resolved_by="charlie")

    pending = q.list_pending()
    assert not any(i.item_id == item.item_id for i in pending)

    s = q.stats()
    assert s["rejected"] == 1
    assert s["pending"] == 0
    q.close()


def test_reject_only_affects_pending(tmp_path: Path) -> None:
    """reject() on a non-pending item_id is a no-op."""
    q = _queue(tmp_path)
    q.reject("nonexistent-id", resolved_by="nobody")
    assert q.stats()["rejected"] == 0
    q.close()


# ---------------------------------------------------------------------------
# stats()
# ---------------------------------------------------------------------------


def test_stats_empty_queue(tmp_path: Path) -> None:
    """stats() on an empty queue returns all zeros."""
    q = _queue(tmp_path)
    s = q.stats()
    assert s == {"pending": 0, "approved": 0, "rejected": 0}
    q.close()


def test_stats_mixed(tmp_path: Path) -> None:
    """stats() correctly counts items in each status."""
    q = _queue(tmp_path)
    items = [_item(f"m-{i}") for i in range(5)]
    for it in items:
        q.enqueue(it)

    q.approve(items[0].item_id, canonical="c0", resolved_by="tester")
    q.approve(items[1].item_id, canonical="c1", resolved_by="tester")
    q.reject(items[2].item_id, resolved_by="tester")

    s = q.stats()
    assert s["pending"] == 2
    assert s["approved"] == 2
    assert s["rejected"] == 1
    q.close()


# ---------------------------------------------------------------------------
# ReviewItem defaults
# ---------------------------------------------------------------------------


def test_review_item_auto_id_and_timestamp() -> None:
    """ReviewItem generates a UUID item_id and timestamps itself."""
    from novafabric.kg.review_queue import ReviewItem

    item = ReviewItem(alias="auto", entity_type="model")
    assert len(item.item_id) == 36  # UUID4 format
    assert item.status == "pending"
    assert item.created_at is not None
    assert item.resolved_at is None


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_enqueue_is_safe(tmp_path: Path) -> None:
    """Concurrent enqueues from 8 threads produce 8 distinct pending items."""
    q = _queue(tmp_path)
    errors: list[str] = []

    def worker(n: int) -> None:
        try:
            q.enqueue(_item(f"thread-model-{n}"))
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    items = q.list_pending()
    assert len(items) == 8
    q.close()


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Enqueued items survive close + re-open of the queue writer."""
    from novafabric.kg.review_queue import HumanReviewQueueWriter

    db = tmp_path / "persist.db"
    q1 = HumanReviewQueueWriter(db_path=db)
    q1.enqueue(_item("persist-model"))
    q1.close()

    q2 = HumanReviewQueueWriter(db_path=db)
    items = q2.list_pending()
    assert len(items) == 1
    assert items[0].alias == "persist-model"
    q2.close()


# ── review eval-adapters-query-20: one pending item per unresolved alias ─────


class _Unresolved:
    def resolve(self, name: str, entity_type: str) -> None:
        return None

    def register(self, entry: object) -> None:  # never reached at confidence 0.5
        raise AssertionError("auto-registered")


def _pipeline(queue: object) -> object:
    from unittest.mock import MagicMock

    from novafabric.kg.pipeline import KGIngestionPipeline

    return KGIngestionPipeline(MagicMock(), alias_resolver=_Unresolved(), review_queue=queue)  # type: ignore[arg-type]


def _event() -> dict[str, str]:
    return {"event_type": "ModelCallCompleted", "agent_id": "agent-x",
            "model_id": "mystery-model", "capsule_id": "cap-1"}


def test_every_event_naming_one_unresolved_alias_queues_it_once(tmp_path: Path) -> None:
    """A fresh UUID per event defeated INSERT OR IGNORE: one row per event, and a
    list_pending() scan after each (unbounded growth, O(n²))."""
    queue = _queue(tmp_path)
    pipeline = _pipeline(queue)
    for _ in range(50):
        pipeline.ingest_event(_event())  # type: ignore[attr-defined]
    assert sorted((i.alias, i.entity_type) for i in queue.list_pending()) == [
        ("agent-x", "agent"),
        ("mystery-model", "model"),
    ]


def test_a_second_ingest_run_does_not_queue_a_pending_alias_again(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    _pipeline(queue).ingest_event(_event())  # type: ignore[attr-defined]
    _pipeline(queue).ingest_event(_event())  # type: ignore[attr-defined]
    assert queue.count_pending() == 2


def test_an_alias_rejected_earlier_is_queued_again(tmp_path: Path) -> None:
    """Only a *pending* item suppresses a new one: a resolved alias that shows up
    unresolved again needs a fresh review."""
    queue = _queue(tmp_path)
    _pipeline(queue).ingest_event(_event())  # type: ignore[attr-defined]
    for item in queue.list_pending():
        queue.reject(item.item_id, resolved_by="r")
    _pipeline(queue).ingest_event(_event())  # type: ignore[attr-defined]
    assert queue.count_pending() == 2


def test_has_pending(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    queue.enqueue(_item("m"))
    assert queue.has_pending("m", "model") is True
    assert queue.has_pending("m", "tool") is False
    assert queue.has_pending("other", "model") is False
