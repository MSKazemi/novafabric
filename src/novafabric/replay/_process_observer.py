"""Report -- never block -- child processes the replayed process starts (ADR-0319).

The implementation lives in :mod:`novafabric.replay._child_observer`, which is
standard-library only so that its source can also run as the bootstrap of Python
child processes (#52 part 2). This module keeps the original import path.
"""

from __future__ import annotations

from novafabric.replay._child_observer import (
    CHILD_PROCESS_EVENT_CAP,
    ProcessObserver,
    program_name,
)

__all__ = ["CHILD_PROCESS_EVENT_CAP", "ProcessObserver", "program_name"]
