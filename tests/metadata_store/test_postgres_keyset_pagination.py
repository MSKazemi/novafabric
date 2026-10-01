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
"""ADR-0206 P2 — the Postgres half of keyset pagination.

Two layers:

* **No database** — the shared ``metadata_store._keyset`` helpers and the SQL
  ``PostgresMetadataStore.query_runs`` builds, driven through a recording fake
  connection. These run everywhere.
* **Real Postgres** (``postgres_url``, container tier — skipped without Docker
  or ``NOVA_TEST_POSTGRES_DSN``) — seeded walks against a full sorted scan,
  duplicate timestamps, inserts/deletes between pages, legacy-cursor migration
  and tampered cursors, mirroring ``test_sqlite_keyset_pagination.py``.
"""

from __future__ import annotations

import base64
import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from novafabric.metadata_store import _keyset
from novafabric.metadata_store import postgres as pg_mod
from novafabric.metadata_store.postgres import PostgresMetadataStore
from novafabric.server.pagination import InvalidCursorError, encode_keyset_cursor

T0 = datetime(2026, 5, 13, 0, 0, tzinfo=timezone.utc)
# Fixed, not uuid4(): parametrize ids must be identical on every xdist worker.
_FIXED_ID = "6f1c2a9e-0b7d-4c3e-9a51-2d8e4f7b1c30"


def _decode(cursor: str) -> dict[str, Any]:
    padded = cursor + "=" * (-len(cursor) % 4)
    data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return data


# ── shared helpers (no database) ───────────────────────────────────────────


def test_seek_predicate_sqlite_text_is_unchanged() -> None:
    sql, params = _keyset.seek_predicate(("2026-01-01", "r"))
    assert sql == "(started_at < ? OR (started_at = ? AND run_id < ?) OR started_at IS NULL)"
    assert params == ["2026-01-01", "2026-01-01", "r"]
    sql, params = _keyset.seek_predicate((None, "r"))
    assert sql == "(started_at IS NULL AND run_id < ?)" and params == ["r"]


def test_seek_predicate_postgres_text_binds_and_casts() -> None:
    sql, params = _keyset.seek_predicate(
        ("2026-01-01T00:00:00+00:00", "x"),
        placeholder="%s",
        started_at_cast="::timestamptz",
        run_id_cast="::uuid",
    )
    assert sql == (
        "(started_at < %s::timestamptz OR (started_at = %s::timestamptz AND "
        "run_id < %s::uuid) OR started_at IS NULL)"
    )
    assert len(params) == 3 and "2026" not in sql


@pytest.mark.parametrize(
    ("key", "message"),
    [
        (("infinity", _FIXED_ID), "not an ISO-8601"),
        (("now", _FIXED_ID), "not an ISO-8601"),
        (("2026-01-01T00:00:00", _FIXED_ID), "no UTC offset"),
        (("2026-01-01T00:00:00Z", "not-a-uuid"), "not a UUID"),
    ],
)
def test_validate_typed_key_rejects(key: tuple[str, str], message: str) -> None:
    with pytest.raises(InvalidCursorError, match=message):
        _keyset.validate_typed_key(key)


def test_validate_typed_key_accepts_sqlite_and_postgres_forms() -> None:
    rid = str(uuid4())
    for stamp in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.123456+02:00", None):
        assert _keyset.validate_typed_key((stamp, rid)) == (stamp, rid)


def test_timestamp_key_round_trips_microseconds() -> None:
    value = datetime(2026, 5, 13, 1, 2, 3, 456789, tzinfo=timezone.utc)
    key = _keyset.timestamp_key(value)
    assert key == "2026-05-13T01:02:03.456789+00:00"
    assert key is not None and datetime.fromisoformat(key) == value
    assert _keyset.timestamp_key(None) is None
    assert _keyset.timestamp_key("s") == "s"
    assert _keyset.timestamp_key(7) == "7"


@pytest.mark.parametrize("bad", ["9" * 25, str(2**63)])
def test_legacy_offset_out_of_range(bad: str) -> None:
    with pytest.raises(InvalidCursorError):
        _keyset.parse_store_cursor(bad)


# ── query_runs SQL through a recording fake connection ────────────────────


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, list[Any]]] = []

    def execute(self, sql: str, params: list[Any]) -> _FakeResult:
        self.calls.append((sql, list(params)))
        return _FakeResult(self.rows)


@contextmanager
def _active(conn: _FakeConn) -> Iterator[None]:
    token = pg_mod._active_conn_var.set(conn)  # type: ignore[arg-type]
    try:
        yield
    finally:
        pg_mod._active_conn_var.reset(token)


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {"run_id": UUID(int=n - i), "started_at": T0 - timedelta(minutes=i)} for i in range(n)
    ]


def test_first_page_sql_and_cursor() -> None:
    conn = _FakeConn(_rows(4))
    store = PostgresMetadataStore(dsn="postgresql://unused")
    with _active(conn):
        page, cursor = store.query_runs(uuid4(), limit=3)
    sql, params = conn.calls[0]
    assert "ORDER BY started_at DESC NULLS LAST, run_id DESC" in sql
    assert "LIMIT %s OFFSET %s" in sql and params[-2:] == [4, 0]
    assert len(page) == 3 and cursor is not None
    assert _decode(cursor) == {
        "v": 1,
        "k": [(T0 - timedelta(minutes=2)).isoformat(), str(UUID(int=2))],
    }


def test_keyset_page_binds_seek() -> None:
    conn = _FakeConn(_rows(2))
    store = PostgresMetadataStore(dsn="postgresql://unused")
    rid = str(uuid4())
    cursor = encode_keyset_cursor("2026-05-13T00:00:00Z", rid)
    with _active(conn):
        page, nxt = store.query_runs(uuid4(), limit=5, cursor=cursor)
    sql, params = conn.calls[0]
    assert "started_at < %s::timestamptz" in sql
    assert params[1:4] == ["2026-05-13T00:00:00Z", "2026-05-13T00:00:00Z", rid]
    assert params[-1] == 0
    assert len(page) == 2 and nxt is None


def test_legacy_bare_integer_cursor_uses_offset_then_migrates() -> None:
    conn = _FakeConn(_rows(3))
    store = PostgresMetadataStore(dsn="postgresql://unused")
    with _active(conn):
        _, nxt = store.query_runs(uuid4(), limit=2, cursor="50")
    sql, params = conn.calls[0]
    assert "::timestamptz" not in sql and params[-2:] == [3, 50]
    assert nxt is not None and _decode(nxt)["v"] == 1


@pytest.mark.parametrize(
    "cursor",
    [
        "garbage!!",
        encode_keyset_cursor("infinity", _FIXED_ID),
        encode_keyset_cursor("2026-05-13T00:00:00Z", "1; DROP TABLE runs"),
        "-5",
    ],
)
def test_invalid_cursor_raises_before_touching_the_database(cursor: str) -> None:
    conn = _FakeConn([])
    store = PostgresMetadataStore(dsn="postgresql://unused")
    with _active(conn), pytest.raises(InvalidCursorError):
        store.query_runs(uuid4(), cursor=cursor)
    assert conn.calls == []


def test_limit_below_one_is_clamped() -> None:
    conn = _FakeConn(_rows(2))
    store = PostgresMetadataStore(dsn="postgresql://unused")
    with _active(conn):
        page, nxt = store.query_runs(uuid4(), limit=0)
    assert len(page) == 1 and nxt is not None
    assert conn.calls[0][1][-2:] == [2, 0]


# ── real Postgres (container tier) ────────────────────────────────────────


def _store(postgres_url: str) -> PostgresMetadataStore:
    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()
    return store


def _seed(ctx: Any, tenant: UUID, n: int, rng: random.Random) -> None:
    # Few distinct timestamps ⇒ many ties: the run_id tiebreak is exercised.
    for _ in range(n):
        ctx.register_run(
            uuid4(),
            tenant,
            event_type="run.started",
            started_at=(T0 + timedelta(seconds=rng.randrange(4))).isoformat(),
        )


def _walk(ctx: Any, tenant: UUID, limit: int) -> list[str]:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10_000):
        page, cursor = ctx.query_runs(tenant, limit=limit, cursor=cursor)
        seen.extend(str(r["run_id"]) for r in page)
        if cursor is None:
            return seen
    raise AssertionError("walk did not terminate")


def _full_order(ctx: Any, tenant: UUID) -> list[str]:
    rows = ctx._conn().execute(
        "SELECT run_id FROM runs WHERE tenant_id = %s "
        "ORDER BY started_at DESC NULLS LAST, run_id DESC",
        (str(tenant),),
    ).fetchall()
    return [str(r["run_id"]) for r in rows]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_pg_walk_matches_full_scan(postgres_url: str, seed: int) -> None:
    store = _store(postgres_url)
    tenant = uuid4()
    with store.begin_tenant_context(tenant) as ctx:
        _seed(ctx, tenant, 23, random.Random(seed))
        walked = _walk(ctx, tenant, limit=4)
        assert walked == _full_order(ctx, tenant)
        assert len(set(walked)) == 23


def test_pg_walk_is_stable_under_insert_and_delete(postgres_url: str) -> None:
    store = _store(postgres_url)
    tenant = uuid4()
    with store.begin_tenant_context(tenant) as ctx:
        _seed(ctx, tenant, 12, random.Random(7))
        page1, cursor = ctx.query_runs(tenant, limit=5)
        assert cursor is not None
        served = {str(r["run_id"]) for r in page1}
        survivors = [r for r in _full_order(ctx, tenant) if r not in served]
        doomed = survivors[0]
        ctx._conn().execute("DELETE FROM runs WHERE run_id = %s", (doomed,))
        # A newer row lands "before" the cursor: it must not appear later.
        ctx.register_run(
            uuid4(), tenant, event_type="run.started",
            started_at=(T0 + timedelta(hours=1)).isoformat(),
        )
        rest: list[str] = []
        while cursor is not None:
            page, cursor = ctx.query_runs(tenant, limit=5, cursor=cursor)
            rest.extend(str(r["run_id"]) for r in page)
        assert set(rest) == set(survivors) - {doomed}
        assert not served & set(rest)


def test_pg_legacy_offset_cursor_migrates(postgres_url: str) -> None:
    store = _store(postgres_url)
    tenant = uuid4()
    with store.begin_tenant_context(tenant) as ctx:
        _seed(ctx, tenant, 9, random.Random(11))
        full = _full_order(ctx, tenant)
        page, cursor = ctx.query_runs(tenant, limit=3, cursor="3")
        assert [str(r["run_id"]) for r in page] == full[3:6]
        assert cursor is not None and _decode(cursor)["v"] == 1
        page, _ = ctx.query_runs(tenant, limit=3, cursor=cursor)
        assert [str(r["run_id"]) for r in page] == full[6:9]


def test_pg_accepts_a_sqlite_shaped_cursor(postgres_url: str) -> None:
    """A ``Z``-suffixed SQLite-style key seeks to the same position."""
    store = _store(postgres_url)
    tenant = uuid4()
    with store.begin_tenant_context(tenant) as ctx:
        _seed(ctx, tenant, 6, random.Random(5))
        full = _full_order(ctx, tenant)
        row = ctx.lookup_run(UUID(full[1]), tenant)
        assert row is not None
        stamp = row["started_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        page, _ = ctx.query_runs(tenant, limit=10, cursor=encode_keyset_cursor(stamp, full[1]))
        assert [str(r["run_id"]) for r in page] == full[2:]


def test_pg_tampered_cursor_raises(postgres_url: str) -> None:
    store = _store(postgres_url)
    tenant = uuid4()
    with store.begin_tenant_context(tenant) as ctx, pytest.raises(InvalidCursorError):
        ctx.query_runs(tenant, cursor=encode_keyset_cursor("now", str(uuid4())))
