"""Cursor-based pagination helpers for the NovaFabric REST API.

Two cursor formats coexist (ADR-0206, experimental):

* **v1 keyset** — base64url JSON ``{"v": 1, "k": [created_at, id]}``: the
  sort key of the last row of a page under the fixed order
  ``created_at DESC, id DESC``. Opaque to clients; strictly decoded.
* **legacy v0 offset** — base64url JSON ``{"offset": N}``: accepted for one
  deprecation cycle (ADR-0188) and served by the old materialized-list path.

:func:`parse_cursor` is the strict decoder used by the capsules list route:
a non-empty cursor that fails to parse raises :class:`InvalidCursorError`
(→ 400 ``invalid_cursor``), replacing the old silent restart-at-offset-0.
:func:`decode_cursor` keeps the legacy lenient behavior for the routes that
have not moved to keyset yet (assets, lineage — ADR-0206 P2).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

T = TypeVar("T")

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


class InvalidCursorError(Exception):
    """A non-empty cursor failed strict decoding (ADR-0206 D1)."""


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


@dataclass(frozen=True)
class ParsedCursor:
    """Result of strict cursor parsing.

    ``kind`` is ``"first"`` (absent/empty cursor), ``"keyset"`` (v1 — ``key``
    holds the ``(created_at, id)`` seek position; ``created_at`` may be None
    for the NULL tail), or ``"offset"`` (legacy v0 — ``offset`` holds N).
    """

    kind: Literal["first", "keyset", "offset"]
    offset: int = 0
    key: tuple[str | None, str] | None = None


def encode_keyset_cursor(created_at: str | None, id_: str) -> str:
    """Encode a v1 keyset cursor ``{"v": 1, "k": [created_at, id]}``."""
    payload = json.dumps({"v": 1, "k": [created_at, id_]}).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def parse_cursor(cursor: str | None) -> ParsedCursor:
    """Strictly parse a cursor into a :class:`ParsedCursor`.

    Absent/empty → first page. A v1 cursor must carry ``v == 1`` and a
    two-element ``k`` of ``[str|null, str]``. A legacy v0 cursor must carry a
    non-negative integer ``offset``. Anything else — undecodable base64,
    non-JSON, unknown version, malformed key — raises
    :class:`InvalidCursorError` (ADR-0206: corrupted cursors fail loudly
    instead of silently restarting iteration).
    """
    if not cursor:
        return ParsedCursor(kind="first")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = base64.urlsafe_b64decode(padded.encode())
        data = json.loads(payload)
    except Exception as exc:  # noqa: BLE001 — every parse failure is a 400
        raise InvalidCursorError(f"cursor is not decodable: {exc}") from exc
    if not isinstance(data, dict):
        raise InvalidCursorError("cursor payload is not an object")
    if "v" in data:
        if data["v"] != 1:
            raise InvalidCursorError(f"unknown cursor version {data['v']!r}")
        key = data.get("k")
        if (
            not isinstance(key, list)
            or len(key) != 2
            or not (key[0] is None or isinstance(key[0], str))
            or not isinstance(key[1], str)
            or not key[1]
        ):
            raise InvalidCursorError("malformed keyset cursor key")
        return ParsedCursor(kind="keyset", key=(key[0], key[1]))
    if "offset" in data:
        try:
            offset = int(data["offset"])
        except (TypeError, ValueError) as exc:
            raise InvalidCursorError("malformed offset cursor") from exc
        if offset < 0:
            raise InvalidCursorError("negative offset cursor")
        return ParsedCursor(kind="offset", offset=offset)
    raise InvalidCursorError("cursor carries neither 'v' nor 'offset'")


def clamp_limit(limit: int) -> int:
    """Clamp limit to [1, MAX_LIMIT]."""
    return max(1, min(limit, _MAX_LIMIT))


def encode_keyset(after_key: str) -> str:
    """Encode a keyset cursor carrying the last-seen sort key.

    Unlike :func:`encode_cursor` (a positional offset), a keyset cursor names
    *where in the sort order* the next page begins, so concurrent inserts and
    deletes between page fetches can't shift rows into or out of view (S4,
    ADR-0199). The key is an opaque string to callers.
    """
    payload = json.dumps({"after": after_key}).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def decode_keyset(cursor: str | None) -> str | None:
    """Decode a keyset cursor back to its last-seen key.

    Returns ``None`` if the cursor is None, empty, or unparseable (best-effort;
    a bad cursor restarts from the first page rather than raising).
    """
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        key = data["after"]
        return str(key) if key is not None else None
    except Exception:  # noqa: BLE001 — any parse failure → start from the top
        return None


def keyset_page(
    items: list[T],
    limit: int,
    cursor: str | None,
    key_fn: Any,
    *,
    descending: bool = True,
) -> tuple[list[T], str | None]:
    """Page *items* by keyset, returning ``(page, next_cursor_or_None)``.

    *items* MUST already be sorted by ``key_fn`` in display order (descending
    by default). Only items strictly *after* the cursor key are considered, so
    paging is stable across mutations. ``next_cursor`` is None when the page
    reaches the end.
    """
    limit = clamp_limit(limit)
    after = decode_keyset(cursor)
    if after is not None:
        if descending:
            remaining = [it for it in items if str(key_fn(it)) < after]
        else:
            remaining = [it for it in items if str(key_fn(it)) > after]
    else:
        remaining = items
    page = remaining[:limit]
    if len(remaining) > limit and page:
        return page, encode_keyset(str(key_fn(page[-1])))
    return page, None


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
