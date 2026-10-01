# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Keyset pagination shared by every ``MetadataStore.query_runs`` (ADR-0206 P2).

One cursor format and one seek predicate for both backends, so a cursor
emitted by the SQLite store and one emitted by the Postgres store mean the
same thing: the v1 ``server.pagination`` cursor ``{"v": 1, "k": [started_at,
run_id]}`` naming the last row served, under the total order
``started_at DESC NULLS LAST, run_id DESC``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from novafabric.server.pagination import InvalidCursorError, ParsedCursor, parse_cursor

#: Total order for keyset pagination. ``run_id`` is the tiebreak that makes
#: duplicate ``started_at`` values page deterministically.
RUNS_ORDER_BY = "started_at DESC NULLS LAST, run_id DESC"

#: OFFSET is a signed 64-bit integer in both SQLite and Postgres; larger
#: legacy offsets are garbage.
MAX_LEGACY_OFFSET = 2**63 - 1
#: A legacy bare-integer cursor never needs more digits than the max offset.
MAX_LEGACY_OFFSET_DIGITS = len(str(MAX_LEGACY_OFFSET))


def parse_store_cursor(cursor: str | None) -> ParsedCursor:
    """Parse a ``query_runs`` cursor, accepting the pre-P2 bare-integer form.

    The bare-integer offset string (``"50"``) is what both backends emitted
    before ADR-0206 P2; it is recognised first (a v1 cursor is base64 JSON and
    always starts with ``eyJ``, so the forms cannot collide). Everything else
    goes through the shared strict decoder in ``server.pagination``.

    Raises:
        InvalidCursorError: undecodable, unknown-version, malformed, negative
            or out-of-range cursor.
    """
    if cursor is not None and cursor.isascii() and cursor.isdigit():
        if len(cursor) > MAX_LEGACY_OFFSET_DIGITS or int(cursor) > MAX_LEGACY_OFFSET:
            raise InvalidCursorError("legacy offset cursor out of range")
        return ParsedCursor(kind="offset", offset=int(cursor))
    parsed = parse_cursor(cursor)
    if parsed.kind == "offset" and parsed.offset > MAX_LEGACY_OFFSET:
        raise InvalidCursorError("legacy offset cursor out of range")
    return parsed


def seek_predicate(
    key: tuple[str | None, str],
    *,
    placeholder: str = "?",
    started_at_cast: str = "",
    run_id_cast: str = "",
) -> tuple[str, list[Any]]:
    """Return the SQL predicate selecting rows strictly after *key*.

    Under ``started_at DESC NULLS LAST, run_id DESC`` "after" means:

    * cursor in the non-NULL region ``(s, r)``: an older timestamp, or the
      same timestamp with a smaller ``run_id``, **or any NULL-``started_at``
      row** (the whole NULL tail sorts after every non-NULL value);
    * cursor in the NULL tail ``(None, r)``: a NULL-``started_at`` row with a
      smaller ``run_id`` only — never a non-NULL row, which all sort earlier.

    The comparisons are spelled out rather than using a row-value
    ``(started_at, run_id) < (?, ?)``, whose NULL semantics would silently
    drop the NULL tail. ``placeholder`` and the casts adapt the text to the
    driver (``?`` for sqlite3, ``%s`` + ``::timestamptz``/``::uuid`` for
    psycopg); the values are always bound, never interpolated.
    """
    started_at, run_id = key
    p = placeholder
    s, r = f"{p}{started_at_cast}", f"{p}{run_id_cast}"
    if started_at is None:
        return f"(started_at IS NULL AND run_id < {r})", [run_id]
    return (
        f"(started_at < {s} OR (started_at = {s} AND run_id < {r}) OR started_at IS NULL)",
        [started_at, started_at, run_id],
    )


def validate_typed_key(key: tuple[str | None, str]) -> tuple[str | None, str]:
    """Check a v1 key against typed columns (``TIMESTAMPTZ`` / ``UUID``).

    A cursor is caller-supplied input. Without this check a tampered key would
    reach the database as a cast failure (a 500) — or, worse, as a value
    Postgres accepts but no row ever had (``'infinity'``, ``'now'``).

    Raises:
        InvalidCursorError: ``started_at`` is not an ISO-8601 timestamp with a
            UTC offset, or ``run_id`` is not a UUID.
    """
    started_at, run_id = key
    if started_at is not None:
        try:
            parsed = datetime.fromisoformat(started_at)
        except ValueError as exc:
            raise InvalidCursorError("cursor started_at is not an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise InvalidCursorError("cursor started_at carries no UTC offset")
    try:
        UUID(run_id)
    except ValueError as exc:
        raise InvalidCursorError("cursor run_id is not a UUID") from exc
    return started_at, run_id


def timestamp_key(value: Any) -> str | None:
    """Normalise a stored ``started_at`` into the cursor's ``str | None`` slot.

    A ``datetime`` (psycopg ``TIMESTAMPTZ``) becomes its ISO-8601 form with
    microseconds and offset, so it compares exactly equal when cast back.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
