"""Cross-hook layering coordination (RFC-0001 Option C, sub-track C-3.2).

Some HTTP-stack libraries are layered: ``requests`` calls ``urllib3``
internally, ``boto3`` calls ``urllib3`` internally, and so on. Without
coordination, capturing both layers would record the same outbound
request twice — once by the higher hook (``RequestsHook``) and once by
the lower hook (``Urllib3Hook``).

This module provides a single context-local guard that higher-layer
hooks set while they own the call, and lower-layer hooks check before
recording. Using :class:`contextvars.ContextVar` (not :class:`threading.local`)
so the guard propagates correctly through ``asyncio`` tasks too — a
future async hook layered above ``urllib3`` would Just Work.

Design rule: only the **higher** layer needs to know about the lower.
The lower layer only needs to consult the guard.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

# True while a higher-layer hook is processing a call that will reach
# the lower layer. Lower-layer hooks must check this before recording.
_in_higher_layer: ContextVar[bool] = ContextVar(
    "novafabric_in_higher_layer_hook", default=False
)


def is_inside_higher_layer() -> bool:
    """Return True if a higher-layer hook is currently processing a call.

    Lower-layer hooks (``urllib3``, future ``boto3`` if added) consult
    this to skip duplicate recording.
    """
    return _in_higher_layer.get()


@contextmanager
def claim_higher_layer() -> Iterator[None]:
    """Mark the current call as owned by a higher-layer hook.

    Higher-layer hooks (``requests``, future layered hooks) wrap the
    underlying transport call inside this context manager so any
    lower-layer hook fired by the same call sees the guard and skips.

    Reset is via :class:`contextvars.Token` so re-entrancy is safe and
    the guard is restored on exception.
    """
    token = _in_higher_layer.set(True)
    try:
        yield
    finally:
        _in_higher_layer.reset(token)
