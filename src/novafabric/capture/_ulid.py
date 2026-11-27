from __future__ import annotations

import os

from ulid import ULID


def new_ulid() -> str:
    """A new ULID string (26 characters, time-sortable), as used for run ids."""
    return str(ULID())


def new_span_id() -> str:
    """A new random span id: 16 lowercase hex characters (8 random bytes)."""
    return os.urandom(8).hex()
