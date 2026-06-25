from __future__ import annotations

import os

from ulid import ULID


def new_ulid() -> str:
    return str(ULID())


def new_span_id() -> str:
    return os.urandom(8).hex()
