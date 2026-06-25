"""Cursor-based pagination helpers for the NovaFabric REST API.

Cursor format: base64url-encoded JSON ``{"offset": N}``.
This is intentionally simple (offset-based) for the SQLite backend.
A real cursor (keyset) can replace this for the Postgres backend later
without changing the public API shape.
"""

from __future__ import annotations

import base64
import json
from typing import Any, TypeVar

T = TypeVar("T")

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


def encode_cursor(offset: int) -> str:
    """Encode an integer offset as a base64url cursor string."""
    payload = json.dumps({"offset": offset}).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def decode_cursor(cursor: str | None) -> int:
    """Decode a cursor string back to an integer offset.

    Returns 0 if the cursor is None, empty, or invalid (best-effort; no exception).
    """
    if not cursor:
        return 0
    try:
        # Re-add padding stripped during encode
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = base64.urlsafe_b64decode(padded.encode())
        data = json.loads(payload)
        return int(data["offset"])
    except Exception:  # noqa: BLE001 — any parsing failure → start from 0
        return 0


def clamp_limit(limit: int) -> int:
    """Clamp limit to [1, MAX_LIMIT]."""
    return max(1, min(limit, _MAX_LIMIT))


def paginate(items: list[Any], limit: int, offset: int) -> tuple[list[Any], str | None]:
    """Apply pagination to a pre-fetched list.

    Returns ``(page_items, next_cursor_or_None)``.

    ``next_cursor`` is None when there are no more items beyond this page.
    """
    limit = clamp_limit(limit)
    page = items[offset : offset + limit]
    if offset + limit < len(items):
        next_cursor: str | None = encode_cursor(offset + limit)
    else:
        next_cursor = None
    return page, next_cursor
