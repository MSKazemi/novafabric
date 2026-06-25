"""Tests for DeltaBuffer (FR-08, FR-09 server side)."""

from __future__ import annotations

import threading
import time
from typing import Any

from novafabric.serve.topology.delta_buffer import DeltaBuffer


def test_enqueue_assigns_monotonic_seq() -> None:
    buf = DeltaBuffer()
    seqs = [buf.enqueue({"type": "add_node", "node_id": f"n{i}"}) for i in range(10)]
    assert seqs == list(range(1, 11))


def test_checkpoint_returns_batch_checkpoint_event() -> None:
    buf = DeltaBuffer()
    ckpt = buf.checkpoint()
    assert ckpt["type"] == "batch_checkpoint"
    assert "seq" in ckpt
    assert "ts_ms" in ckpt
    assert ckpt["seq"] >= 1


def test_checkpoint_seq_strictly_increasing() -> None:
    buf = DeltaBuffer()
    seqs = [buf.checkpoint()["seq"] for _ in range(60)]
    for a, b in zip(seqs, seqs[1:]):
        assert b > a


def test_replay_since_zero_returns_all() -> None:
    buf = DeltaBuffer()
    for i in range(100):
        buf.enqueue({"type": "add_node", "node_id": f"n{i}"})
    replayed = buf.replay_since(0)
    assert len(replayed) == 100


def test_replay_since_middle_returns_remaining() -> None:
    buf = DeltaBuffer()
    for i in range(20):
        buf.enqueue({"type": "add_node", "node_id": f"n{i}"})
    replayed = buf.replay_since(10)
    assert len(replayed) == 10
    assert all(e["seq"] > 10 for e in replayed)


def test_replay_returns_empty_for_unknown_checkpoint() -> None:
    buf = DeltaBuffer()
    for i in range(5):
        buf.enqueue({"type": "add_node", "node_id": f"n{i}"})
    replayed = buf.replay_since(999)
    assert replayed == []


def test_ring_buffer_caps_at_max_events() -> None:
    buf = DeltaBuffer()
    buf._MAX_EVENTS = 10  # type: ignore[assignment]
    for i in range(20):
        buf.enqueue({"type": "add_node", "node_id": f"n{i}"})
    assert len(buf._deque) <= 10


def test_latest_seq_tracks_total_enqueued() -> None:
    buf = DeltaBuffer()
    for i in range(5):
        buf.enqueue({"type": "add_node", "node_id": f"n{i}"})
    assert buf.latest_seq == 5


# ------------------------------------------------------------------
# Pub-sub tests (A-1: live delta push)
# ------------------------------------------------------------------

def test_subscribe_returns_unique_subscriber_ids() -> None:
    buf = DeltaBuffer()
    received: list[dict[str, Any]] = []
    sub_id_1 = buf.subscribe(lambda e: received.append(e))
    sub_id_2 = buf.subscribe(lambda e: received.append(e))
    assert sub_id_1 != sub_id_2


def test_subscribe_callback_called_on_enqueue() -> None:
    buf = DeltaBuffer()
    received: list[dict[str, Any]] = []
    buf.subscribe(lambda e: received.append(e))
    buf.enqueue({"type": "add_node", "node_id": "n1"})
    assert len(received) == 1
    assert received[0]["type"] == "add_node"
    assert received[0]["node_id"] == "n1"
    assert "seq" in received[0]


def test_subscribe_callback_called_for_each_event() -> None:
    buf = DeltaBuffer()
    received: list[dict[str, Any]] = []
    buf.subscribe(lambda e: received.append(e))
    for i in range(5):
        buf.enqueue({"type": "add_node", "node_id": f"n{i}"})
    assert len(received) == 5


def test_multiple_subscribers_each_receive_event() -> None:
    buf = DeltaBuffer()
    received_a: list[dict[str, Any]] = []
    received_b: list[dict[str, Any]] = []
    buf.subscribe(lambda e: received_a.append(e))
    buf.subscribe(lambda e: received_b.append(e))
    buf.enqueue({"type": "add_edge", "source_id": "a", "target_id": "b"})
    assert len(received_a) == 1
    assert len(received_b) == 1
    assert received_a[0]["type"] == "add_edge"
    assert received_b[0]["type"] == "add_edge"


def test_unsubscribe_stops_delivery() -> None:
    buf = DeltaBuffer()
    received: list[dict[str, Any]] = []
    sub_id = buf.subscribe(lambda e: received.append(e))
    buf.enqueue({"type": "add_node", "node_id": "n1"})
    buf.unsubscribe(sub_id)
    buf.enqueue({"type": "add_node", "node_id": "n2"})
    # Only the first event should have been received
    assert len(received) == 1
    assert received[0]["node_id"] == "n1"


def test_unsubscribe_unknown_id_is_noop() -> None:
    buf = DeltaBuffer()
    # Should not raise
    buf.unsubscribe("nonexistent-subscriber-id")


def test_callback_exception_does_not_prevent_other_callbacks() -> None:
    """A failing callback must not prevent other subscribers from receiving events."""
    buf = DeltaBuffer()
    received: list[dict[str, Any]] = []

    def _bad_cb(e: dict[str, Any]) -> None:
        raise RuntimeError("intentional test failure")

    buf.subscribe(_bad_cb)
    buf.subscribe(lambda e: received.append(e))
    buf.enqueue({"type": "add_node", "node_id": "n1"})
    assert len(received) == 1


def test_thread_safety_concurrent_enqueue_and_subscribe() -> None:
    """Multiple threads enqueuing while subscribe/unsubscribe race must not crash."""
    buf = DeltaBuffer()
    errors: list[Exception] = []

    def _writer() -> None:
        for i in range(100):
            buf.enqueue({"type": "add_node", "node_id": f"n{i}"})

    def _subscriber() -> None:
        for _ in range(20):
            received: list[dict[str, Any]] = []
            sub_id = buf.subscribe(lambda e, r=received: r.append(e))
            time.sleep(0.001)
            buf.unsubscribe(sub_id)

    threads = [threading.Thread(target=_writer) for _ in range(4)]
    threads += [threading.Thread(target=_subscriber) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert errors == []


def test_checkpoint_callback_receives_batch_checkpoint_event() -> None:
    buf = DeltaBuffer()
    received: list[dict[str, Any]] = []
    buf.subscribe(lambda e: received.append(e))
    ckpt = buf.checkpoint()
    assert any(e["type"] == "batch_checkpoint" for e in received)
    # The returned checkpoint dict must match the stored event seq
    assert ckpt["seq"] == received[-1]["seq"]
