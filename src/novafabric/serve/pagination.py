"""Shared pagination helpers for serve/server route groups (ADR-0199).

Two families live here:

* **Keyset cursors** — ``encode_keyset``/``decode_keyset`` wrap the opaque
  URL-safe base64 ``(created_at, run_id)`` cursor used by ``/api/runs`` and
  ``/api/runs/search`` (B-1). The cursor is deliberately opaque to clients;
  a malformed cursor decodes to ``None`` and the caller falls back to the
  first page rather than erroring.
* **Envelope helpers** — ``clamp_limit`` bounds a caller-supplied page size,
  ``page_envelope`` is the canonical ``{items, next_cursor, total,
  truncated}`` response shape for new paginated read endpoints.

Extracted from ``serve/app.py`` so routers (``serve/routers/*``) and the
``server/`` app can share one codec instead of re-implementing it inline.
"""

from __future__ import annotations

import base64
import json
from typing import Any

__all__ = ["clamp_limit", "decode_keyset", "encode_keyset", "page_envelope"]


def encode_keyset(ts: str, run_id: str) -> str:
    """Encode (timestamp, run_id) into an opaque URL-safe base64 string."""
    payload = json.dumps({"ts": ts, "id": run_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_keyset(cursor: str | None) -> tuple[str, str] | None:
    """Decode a cursor string. Returns (ts, run_id) or None if invalid/absent."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode() + b"==")
        obj = json.loads(raw)
        return (obj["ts"], obj["id"])
    except Exception:  # noqa: BLE001 — any malformed cursor means "no cursor"
        return None


def clamp_limit(limit: int, max_: int) -> int:
    """Clamp a caller-supplied page size into [1, max_]."""
    return max(1, min(limit, max_))


def page_envelope(
    items: list[Any],
    next_cursor: Any | None = None,
    total: int | None = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """Canonical paginated-response envelope for new read endpoints.

    ``next_cursor`` is ``None`` on the last page; ``total`` is an optional
    (possibly approximate) full count; ``truncated`` marks responses where a
    bounded read stopped before the underlying data was exhausted.
    """
    return {
        "items": items,
        "next_cursor": next_cursor,
        "total": total,
        "truncated": truncated,
    }
