"""``guard_iterator`` closes the capsule even when the inner generator's own ``close()`` raises.

Found with the real smolagents 1.26.0: its ``run(stream=True)`` generator yields inside a
``finally`` block, so closing it mid-step raises ``RuntimeError: generator ignored
GeneratorExit``. The guard re-raised that before ever reaching ``closer.close()``, leaving the
capsule open and the wire hooks claimed until garbage collection.
"""
from __future__ import annotations

import contextvars
from collections.abc import Iterator
from typing import Any

import pytest

from novafabric.adapters._streaming import Closer, guard_iterator


class _Cap:
    def __init__(self) -> None:
        self.events: list[str] = []

    def fail(self, exc: BaseException) -> None:
        self.events.append(f"fail:{type(exc).__name__}")

    def mark_partial(self, reason: str) -> None:
        self.events.append(f"partial:{reason}")

    def finish(self) -> None:
        self.events.append("finish")


def _yields_in_finally() -> Iterator[int]:
    try:
        yield 1
        yield 2
    finally:
        yield 99  # a generator that cannot be closed cleanly


def test_a_raising_inner_close_still_closes_the_capsule_and_the_error_propagates() -> None:
    cap = _Cap()
    flag: contextvars.ContextVar[bool] = contextvars.ContextVar("flag", default=False)
    guarded = guard_iterator(_yields_in_finally(), Closer(cap), flag)
    assert next(guarded) == 1
    with pytest.raises(RuntimeError, match="ignored GeneratorExit"):
        guarded.close()
    assert cap.events == ["partial:abandoned", "finish"]


def test_a_well_behaved_generator_is_unchanged_by_the_fix() -> None:
    cap = _Cap()
    flag: contextvars.ContextVar[bool] = contextvars.ContextVar("flag2", default=False)
    seen: list[Any] = list(guard_iterator(iter([1, 2]), Closer(cap), flag))
    assert seen == [1, 2] and cap.events == ["finish"]
