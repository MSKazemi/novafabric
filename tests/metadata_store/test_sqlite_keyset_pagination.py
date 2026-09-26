"""ADR-0206 P2 (experimental): keyset pushdown in ``SQLiteMetadataStore.query_runs``.

Acceptance criteria covered here:

* a keyset walk over randomized data (NULL ``started_at``, duplicate
  timestamps, a second tenant interleaved) returns every tenant row exactly
  once, in ``started_at DESC NULLS LAST, run_id DESC`` order, across page
  boundaries — compared against a full sorted scan (property-style, seeded);
* inserts/deletes between pages cause no duplicates, and every row that
  survives the whole walk is still returned exactly once;
* ``next_cursor`` is the shared v1 format from ``server.pagination``;
* legacy offset cursors (bare integer, base64 ``{"offset": N}``) still page;
* tampered / garbage cursors raise ``InvalidCursorError``.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest

from novafabric.metadata_store.sqlite import SQLiteMetadataStore
from novafabric.server.pagination import (
    InvalidCursorError,
    encode_cursor,
    encode_keyset_cursor,
    parse_cursor,
)

# A small timestamp pool forces many duplicate timestamps; None forces a NULL tail.
_TS_POOL: tuple[str | None, ...] = (
    None,
    "2026-01-01T00:00:00Z",
    "2026-01-01T00:00:01Z",
    "2026-03-15T12:00:00Z",
    "2026-09-25T08:30:00Z",
)


@pytest.fixture()
def store(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> SQLiteMetadataStore:
    """A bootstrapped temp-file SQLiteMetadataStore."""
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    with pytest.warns(UserWarning):
        s = SQLiteMetadataStore(db_path=tmp_path / "meta.db")
    s.bootstrap()
    return s


def _uuid(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128), version=4)


def _register(
    store: SQLiteMetadataStore, tenant: UUID, run_id: UUID, started_at: str | None
) -> None:
    store.register_run(run_id, tenant, event_type="run.started", started_at=started_at)


def _oracle(rows: list[tuple[str, str | None]]) -> list[str]:
    """Full sorted scan: ``started_at DESC NULLS LAST, run_id DESC``."""
    ordered = sorted(rows, key=lambda r: (r[1] is not None, r[1] or "", r[0]), reverse=True)
    return [run_id for run_id, _ in ordered]


def _sort_key(row: dict[str, Any]) -> tuple[bool, str, str]:
    return (row["started_at"] is not None, row["started_at"] or "", row["run_id"])


def _walk(
    store: SQLiteMetadataStore, tenant: UUID, limit: int, max_pages: int = 10_000
) -> Iterator[tuple[list[dict[str, Any]], str | None]]:
    cursor: str | None = None
    for _ in range(max_pages):
        page, cursor = store.query_runs(tenant, limit=limit, cursor=cursor)
        yield page, cursor
        if cursor is None:
            return
    raise AssertionError("walk did not terminate")


@pytest.mark.parametrize("seed", range(30))
def test_keyset_walk_matches_full_sorted_scan(store: SQLiteMetadataStore, seed: int) -> None:
    rng = random.Random(seed)
    tenant, other = _uuid(rng), _uuid(rng)
    rows: list[tuple[str, str | None]] = []
    for _ in range(rng.randint(0, 60)):
        rid, ts = _uuid(rng), rng.choice(_TS_POOL)
        _register(store, tenant, rid, ts)
        rows.append((str(rid), ts))
    for _ in range(rng.randint(0, 15)):  # other tenant, interleaved keys
        _register(store, other, _uuid(rng), rng.choice(_TS_POOL))
    limit = rng.randint(1, 7)

    seen: list[str] = []
    for page, cursor in _walk(store, tenant, limit):
        assert len(page) <= limit
        if cursor is not None:
            assert len(page) == limit
            parsed = parse_cursor(cursor)  # shared v1 format, not a fork
            assert parsed.kind == "keyset"
            assert parsed.key == (page[-1]["started_at"], page[-1]["run_id"])
        assert all(r["tenant_id"] == str(tenant) for r in page)
        seen.extend(r["run_id"] for r in page)

    assert seen == _oracle(rows)
    assert len(seen) == len(set(seen))


def test_null_started_at_sorts_last_and_pages_within_tail(
    store: SQLiteMetadataStore,
) -> None:
    tenant = uuid4()
    rows = [(uuid4(), None) for _ in range(5)] + [
        (uuid4(), "2026-01-01T00:00:00Z") for _ in range(3)
    ]
    for rid, ts in rows:
        _register(store, tenant, rid, ts)
    pages = list(_walk(store, tenant, limit=2))
    flat = [r for page, _ in pages for r in page]
    assert [r["started_at"] for r in flat] == ["2026-01-01T00:00:00Z"] * 3 + [None] * 5
    # A cursor positioned inside the NULL tail encodes k[0] = null.
    tail_cursors = [c for _, c in pages if c is not None and parse_cursor(c).key[0] is None]  # type: ignore[index]
    assert tail_cursors
    assert [r["run_id"] for r in flat] == _oracle([(str(r), t) for r, t in rows])


@pytest.mark.parametrize("seed", range(15))
def test_mutations_between_pages_cause_no_duplicates_or_skips(
    store: SQLiteMetadataStore, seed: int
) -> None:
    rng = random.Random(1000 + seed)
    tenant = _uuid(rng)
    live: dict[str, str | None] = {}
    for _ in range(40):
        rid, ts = _uuid(rng), rng.choice(_TS_POOL)
        _register(store, tenant, rid, ts)
        live[str(rid)] = ts
    initial = set(live)
    deleted: set[str] = set()

    seen: list[dict[str, Any]] = []
    for page, cursor in _walk(store, tenant, limit=rng.randint(1, 6)):
        seen.extend(page)
        if cursor is None:
            break
        for _ in range(rng.randint(0, 3)):  # concurrent inserts
            rid, ts = _uuid(rng), rng.choice(_TS_POOL)
            _register(store, tenant, rid, ts)
            live[str(rid)] = ts
        for victim in rng.sample(sorted(live), k=min(len(live), rng.randint(0, 2))):
            with store._connect() as conn:  # concurrent deletes
                conn.execute("DELETE FROM runs WHERE run_id = ?", (victim,))
            live.pop(victim)
            deleted.add(victim)

    ids = [r["run_id"] for r in seen]
    assert len(ids) == len(set(ids)), "keyset walk returned a row twice"
    assert all(_sort_key(a) > _sort_key(b) for a, b in zip(seen, seen[1:])), "order broke"
    survivors = initial - deleted
    assert survivors <= set(ids), "a row present for the whole walk was skipped"


def test_offset_contrast_duplicates_under_insert(store: SQLiteMetadataStore) -> None:
    """Why keyset: the legacy offset path re-serves a row after a newer insert."""
    tenant = uuid4()
    for i in range(4):
        _register(store, tenant, uuid4(), f"2026-01-0{i + 1}T00:00:00Z")
    first, _ = store.query_runs(tenant, limit=2, cursor="0")
    _, keyset_cursor = store.query_runs(tenant, limit=2)
    _register(store, tenant, uuid4(), "2027-01-01T00:00:00Z")  # newest row

    offset_page, _ = store.query_runs(tenant, limit=2, cursor="2")
    keyset_page, _ = store.query_runs(tenant, limit=2, cursor=keyset_cursor)
    first_ids = {r["run_id"] for r in first}
    assert first_ids & {r["run_id"] for r in offset_page}  # offset duplicates
    assert not first_ids & {r["run_id"] for r in keyset_page}  # keyset does not


@pytest.mark.parametrize("legacy", ["3", encode_cursor(3)])
def test_legacy_offset_cursor_still_pages_then_migrates_to_keyset(
    store: SQLiteMetadataStore, legacy: str
) -> None:
    tenant = uuid4()
    rows = [(uuid4(), "2026-05-13T00:00:00Z") for _ in range(8)]
    for rid, ts in rows:
        _register(store, tenant, rid, ts)
    expected = _oracle([(str(r), t) for r, t in rows])

    page, cursor = store.query_runs(tenant, limit=3, cursor=legacy)
    assert [r["run_id"] for r in page] == expected[3:6]
    assert cursor is not None and parse_cursor(cursor).kind == "keyset"
    rest, final = store.query_runs(tenant, limit=10, cursor=cursor)
    assert [r["run_id"] for r in rest] == expected[6:]
    assert final is None


def test_empty_cursor_is_first_page_and_limit_is_clamped(store: SQLiteMetadataStore) -> None:
    tenant = uuid4()
    for _ in range(3):
        _register(store, tenant, uuid4(), None)
    page, cursor = store.query_runs(tenant, limit=0, cursor="")
    assert len(page) == 1 and cursor is not None
    assert store.query_runs(uuid4(), limit=5) == ([], None)


def test_keyset_cursor_past_the_end_returns_empty(store: SQLiteMetadataStore) -> None:
    tenant = uuid4()
    _register(store, tenant, uuid4(), "2026-01-01T00:00:00Z")
    cursor = encode_keyset_cursor(None, "00000000-0000-0000-0000-000000000000")
    assert store.query_runs(tenant, cursor=cursor) == ([], None)


@pytest.mark.parametrize(
    "bad",
    [
        "garbage!!",
        "-1",
        "9" * 30,  # bare-integer legacy offset beyond SQLite's int64 OFFSET
        encode_cursor(-1),
        encode_cursor(2**64),
        "eyJ2IjogMn0",  # {"v": 2}
        "eyJ2IjogMSwgImsiOiBbMSwgIngiXX0",  # {"v": 1, "k": [1, "x"]}
        "eyJ2IjogMSwgImsiOiBbbnVsbCwgIiJdfQ",  # {"v": 1, "k": [null, ""]}
        encode_keyset_cursor("2026-01-01", "x")[:-3] + "!!!",  # tampered
        "e30",  # {}
    ],
)
def test_garbage_or_tampered_cursor_raises(store: SQLiteMetadataStore, bad: str) -> None:
    with pytest.raises(InvalidCursorError):
        store.query_runs(uuid4(), limit=5, cursor=bad)


def test_non_text_started_at_is_normalised_into_the_cursor(
    store: SQLiteMetadataStore,
) -> None:
    """A numeric started_at (TEXT affinity stores it as text) still round-trips."""
    tenant = uuid4()
    for _ in range(3):
        _register(store, tenant, uuid4(), 1_700_000_000)  # type: ignore[arg-type]
    seen = [r["run_id"] for page, _ in _walk(store, tenant, limit=1) for r in page]
    assert len(seen) == len(set(seen)) == 3


def test_started_at_key_normalisation() -> None:
    from novafabric.metadata_store.sqlite import _started_at_key

    assert _started_at_key(None) is None
    assert _started_at_key("2026") == "2026"
    assert _started_at_key(5) == "5"
