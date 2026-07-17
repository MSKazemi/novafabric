"""Topology WebSocket backpressure — bounded queue with drop-oldest policy.

Regression tests for the v0.61 audit finding: the delta-subscriber callback
wrapped only the *scheduling* of ``queue.put_nowait`` in try/except; the put
itself ran later on the event-loop thread, so a slow WS client's full queue
raised ``QueueFull`` uncaught in a loop callback and the delta vanished with
no policy. The enqueue must never raise and must drop the OLDEST delta so
the newest state (including checkpoints) always gets through.
"""

from __future__ import annotations

import asyncio

from novafabric.serve.app import _ws_put_drop_oldest


def test_put_on_queue_with_room_keeps_item_and_reports_no_drop() -> None:
    async def scenario() -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
        dropped = _ws_put_drop_oldest(q, b"a")
        assert dropped is False
        assert q.qsize() == 1
        assert q.get_nowait() == b"a"

    asyncio.run(scenario())


def test_put_on_full_queue_evicts_oldest_and_keeps_newest() -> None:
    async def scenario() -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
        assert _ws_put_drop_oldest(q, b"a") is False
        assert _ws_put_drop_oldest(q, b"b") is False
        dropped = _ws_put_drop_oldest(q, b"c")  # full: evict "a"
        assert dropped is True
        assert q.qsize() == 2
        assert q.get_nowait() == b"b"
        assert q.get_nowait() == b"c"

    asyncio.run(scenario())


def test_put_never_raises_even_under_sustained_overflow() -> None:
    async def scenario() -> None:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
        drops = sum(
            _ws_put_drop_oldest(q, f"m{i}".encode()) for i in range(100)
        )
        assert drops == 99
        assert q.get_nowait() == b"m99"  # newest always wins

    asyncio.run(scenario())
