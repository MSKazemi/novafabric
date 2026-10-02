"""ADR-0206 P2 — ``MetadataStore.delete_run`` and the governed ``delete_runs``.

Acceptance criteria:

* ``delete_run`` removes exactly one run's index rows (runs, capsules, signatures)
  for the given tenant, is idempotent, never touches another tenant's or run's
  rows, and never touches a capsule directory;
* the governed ``delete_runs`` **refuses rather than skips**: a legal hold, an
  unexpired WORM lock or (by default) a sealed capsule blocks the *whole*
  request, nothing is deleted, and every blocked run is named;
* holds always win (also over ``allow_sealed``), a corrupt hold line is a hold,
  a released hold unblocks;
* invalid input (empty, non-UUID, over the ceiling) fails before any effect;
* deletions and refusals are audited in the chained log; the capsule on disk is
  byte-identical afterwards; the base ``MetadataStore`` default is
  ``NotImplementedError``;
* the Postgres implementation mirrors this (real-database tests need
  ``NOVA_TEST_POSTGRES_DSN`` / Docker and skip without them; its SQL is also
  checked here against a recording connection).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from novafabric.audit import AuditEventType, AuditLog
from novafabric.metadata_store.interface import MetadataStore
from novafabric.metadata_store.postgres import PostgresMetadataStore
from novafabric.metadata_store.run_delete import (
    MAX_RUNS_PER_REQUEST,
    RunDeleteRefusedError,
    check_runs_deletable,
    delete_runs,
)
from novafabric.metadata_store.sqlite import SQLiteMetadataStore
from novafabric.storage._local_worm import LocalWormAdapter

TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SQLiteMetadataStore:
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)
    with pytest.warns(UserWarning):
        s = SQLiteMetadataStore(db_path=tmp_path / "meta.db")
    s.bootstrap()
    return s


@pytest.fixture()
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "capsules"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def audit(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


def _audit_events(audit: AuditLog) -> list[dict[str, Any]]:
    path = audit._path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _rows(store: SQLiteMetadataStore, table: str) -> int:
    with sqlite3.connect(store._db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608


def _seed(store: SQLiteMetadataStore, tenant: UUID = TENANT, n: int = 1) -> list[UUID]:
    ids = [uuid4() for _ in range(n)]
    for rid in ids:
        store.register_run(rid, tenant, event_type="run.started", started_at="2026-10-01T00:00:00Z")
        store.record_signature(rid, tenant, "sig-" + str(rid)[:8], {"k": "v"})
        with sqlite3.connect(store._db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO capsules (capsule_uri, run_id, tenant_id) VALUES (?,?,?)",
                (f"file:///c/{rid}", str(rid), str(tenant)),
            )
    return ids


def _make_capsule(capsule_dir: Path, run_id: UUID, *, sealed: bool = False) -> Path:
    cap = capsule_dir / str(run_id)
    cap.mkdir()
    (cap / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    if sealed:
        (cap / ".seal").mkdir()
        (cap / ".seal" / "seal.json").write_text("{}")
    return cap


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def _place_hold(capsule_dir: Path, *, released: bool = False, raw: str | None = None) -> None:
    reg = capsule_dir.parent / "registries" / "main"
    reg.mkdir(parents=True, exist_ok=True)
    line = raw or json.dumps(
        {
            "hold_id": "hold-abc12345",
            "registry": "main",
            "reason": "litigation",
            "created_at": "2026-10-01T00:00:00+00:00",
            "released_at": "2026-10-02T00:00:00+00:00" if released else None,
        }
    )
    with (reg / "holds.jsonl").open("a") as fh:
        fh.write(line + "\n")


# ── store layer: delete_run ─────────────────────────────────────────────────


def test_delete_run_removes_all_index_rows_for_that_run(store: SQLiteMetadataStore) -> None:
    keep, gone = _seed(store, n=2)
    assert store.delete_run(gone, TENANT) == 1
    assert store.lookup_run(gone, TENANT) is None
    assert store.lookup_run(keep, TENANT) is not None
    assert (_rows(store, "runs"), _rows(store, "capsules"), _rows(store, "signatures")) == (
        1,
        1,
        1,
    )


def test_delete_run_is_idempotent(store: SQLiteMetadataStore) -> None:
    (rid,) = _seed(store)
    assert store.delete_run(rid, TENANT) == 1
    assert store.delete_run(rid, TENANT) == 0
    assert store.delete_run(uuid4(), TENANT) == 0


def test_delete_run_is_tenant_scoped(store: SQLiteMetadataStore) -> None:
    (rid,) = _seed(store, TENANT)
    assert store.delete_run(rid, OTHER_TENANT) == 0
    assert store.lookup_run(rid, TENANT) is not None
    assert _rows(store, "signatures") == 1


def test_delete_run_keeps_keyset_paging_consistent(store: SQLiteMetadataStore) -> None:
    ids = _seed(store, n=5)
    page, cursor = store.query_runs(TENANT, limit=2)
    store.delete_run(UUID(page[0]["run_id"]), TENANT)
    rest: list[str] = []
    while cursor:
        more, cursor = store.query_runs(TENANT, limit=2, cursor=cursor)
        rest.extend(r["run_id"] for r in more)
    assert page[0]["run_id"] not in rest
    assert len(set(rest)) == len(rest)
    assert set(rest) | {page[1]["run_id"]} <= {str(i) for i in ids}


def test_base_class_default_is_not_implemented() -> None:
    class _Bare(MetadataStore):  # implements only the abstract methods
        def register_run(self, run_id: UUID, tenant_id: UUID, **fields: Any) -> None: ...
        def lookup_run(self, run_id: UUID, tenant_id: UUID) -> dict[str, Any] | None:
            return None
        def query_runs(self, tenant_id: UUID, *, limit: int = 50, cursor: str | None = None,
                       **filters: Any) -> tuple[list[dict[str, Any]], str | None]:
            return [], None
        def begin_tenant_context(self, tenant_id: UUID) -> Any: ...
        def record_signature(self, run_id: UUID, tenant_id: UUID, signature_hash: str,
                             payload: dict[str, Any]) -> None: ...
        def bootstrap(self) -> None: ...
        def health_check(self) -> dict[str, Any]:
            return {}

    with pytest.raises(NotImplementedError, match="_Bare"):
        _Bare().delete_run(uuid4(), TENANT)


# ── governed delete: success ────────────────────────────────────────────────


def test_delete_single_run_is_audited(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    report = delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="alice",
                         audit_log=audit)
    assert report.deleted == [str(rid)] and report.absent == []
    assert store.lookup_run(rid, TENANT) is None
    events = _audit_events(audit)
    assert [e["event_type"] for e in events] == [AuditEventType.RUN_INDEX_DELETE.value]
    assert events[0]["actor"] == "alice"
    assert events[0]["resource_id"] == str(rid)
    assert events[0]["details"]["scope"] == "metadata_index"


def test_bulk_delete_audits_each_run_and_a_summary(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    ids = _seed(store, n=3)
    ghost = uuid4()  # not indexed: idempotent, reported as absent
    report = delete_runs(store, TENANT, [*ids, ghost, ids[0]], capsule_dir=capsule_dir,
                         actor="alice", audit_log=audit)
    assert report.deleted == [str(i) for i in ids]
    assert report.absent == [str(ghost)]
    assert len(report.requested) == 4  # duplicate collapsed
    events = _audit_events(audit)
    assert len(events) == 3 + 1
    summary = events[-1]["details"]
    assert summary["summary"] is True and summary["deleted"] == 3 and summary["absent"] == 1
    assert _rows(store, "runs") == 0


def test_dry_run_changes_nothing_and_audits_nothing(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    report = delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="a",
                         audit_log=audit, dry_run=True)
    assert report.dry_run and report.deleted == []
    assert store.lookup_run(rid, TENANT) is not None
    assert _audit_events(audit) == []


def test_capsule_on_disk_is_never_touched(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    cap = _make_capsule(capsule_dir, rid)
    before = _tree_digest(cap)
    delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="a", audit_log=audit)
    assert cap.is_dir() and _tree_digest(cap) == before


def test_accepts_string_ids(store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog) -> None:
    (rid,) = _seed(store)
    report = delete_runs(store, TENANT, [str(rid)], capsule_dir=capsule_dir, actor="a",
                         audit_log=audit)
    assert report.deleted == [str(rid)]


# ── governed delete: refusal (never skip) ───────────────────────────────────


def test_legal_hold_refuses_everything_and_deletes_nothing(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    ids = _seed(store, n=3)
    _place_hold(capsule_dir)
    with pytest.raises(RunDeleteRefusedError) as exc:
        delete_runs(store, TENANT, ids, capsule_dir=capsule_dir, actor="a", audit_log=audit)
    assert {r.run_id for r in exc.value.refusals} == {str(i) for i in ids}  # all named
    assert {r.code for r in exc.value.refusals} == {"legal_hold_active"}
    assert exc.value.refusals[0].details["hold_ids"] == ["hold-abc12345"]
    assert _rows(store, "runs") == 3 and _rows(store, "signatures") == 3  # nothing deleted
    events = _audit_events(audit)
    assert [e["event_type"] for e in events] == [AuditEventType.RUN_INDEX_DELETE_REFUSED.value]
    assert len(events[0]["details"]["refusals"]) == 3


def test_hold_wins_even_over_allow_sealed(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    _make_capsule(capsule_dir, rid, sealed=True)
    _place_hold(capsule_dir)
    with pytest.raises(RunDeleteRefusedError):
        delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="a",
                    audit_log=audit, allow_sealed=True)
    assert store.lookup_run(rid, TENANT) is not None


def test_released_hold_does_not_block(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    _place_hold(capsule_dir, released=True)
    assert delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="a",
                       audit_log=audit).deleted == [str(rid)]


def test_corrupt_hold_line_fails_closed(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    _place_hold(capsule_dir, raw="{not json")
    with pytest.raises(RunDeleteRefusedError):
        delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="a", audit_log=audit)
    assert store.lookup_run(rid, TENANT) is not None


def test_worm_lock_refuses_only_when_a_run_is_locked_and_blocks_whole_request(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    locked, free = _seed(store, n=2)
    reg = capsule_dir.parent / "registries" / "main"
    reg.mkdir(parents=True)
    LocalWormAdapter(reg / "worm.db").put(str(locked), b"x", retention_days=30)
    with pytest.raises(RunDeleteRefusedError) as exc:
        delete_runs(store, TENANT, [free, locked], capsule_dir=capsule_dir, actor="a",
                    audit_log=audit)
    assert [(r.run_id, r.code) for r in exc.value.refusals] == [(str(locked), "worm_hold")]
    assert store.lookup_run(free, TENANT) is not None  # the free sibling was NOT deleted


def test_sealed_capsule_refused_by_default(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    cap = _make_capsule(capsule_dir, rid, sealed=True)
    before = _tree_digest(cap)
    with pytest.raises(RunDeleteRefusedError) as exc:
        delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="a", audit_log=audit)
    assert exc.value.refusals[0].code == "sealed_capsule"
    assert store.lookup_run(rid, TENANT) is not None
    assert _tree_digest(cap) == before


def test_sealed_capsule_index_row_removable_with_explicit_opt_in(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    cap = _make_capsule(capsule_dir, rid, sealed=True)
    before = _tree_digest(cap)
    report = delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="a",
                         audit_log=audit, allow_sealed=True)
    assert report.deleted == [str(rid)]
    assert _tree_digest(cap) == before  # sealed evidence untouched


def test_unsealed_capsule_present_is_deletable(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    _make_capsule(capsule_dir, rid)
    assert delete_runs(store, TENANT, [rid], capsule_dir=capsule_dir, actor="a",
                       audit_log=audit).deleted == [str(rid)]


def test_one_sealed_among_many_blocks_all(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    a, b = _seed(store, n=2)
    _make_capsule(capsule_dir, b, sealed=True)
    with pytest.raises(RunDeleteRefusedError) as exc:
        delete_runs(store, TENANT, [a, b], capsule_dir=capsule_dir, actor="a", audit_log=audit)
    assert [r.run_id for r in exc.value.refusals] == [str(b)]
    assert _rows(store, "runs") == 2


def test_check_runs_deletable_is_read_only(store: SQLiteMetadataStore, capsule_dir: Path) -> None:
    (rid,) = _seed(store)
    _place_hold(capsule_dir)
    assert [r.code for r in check_runs_deletable([rid], capsule_dir=capsule_dir)] == [
        "legal_hold_active"
    ]
    assert store.lookup_run(rid, TENANT) is not None


# ── governed delete: input validation (no effect) ───────────────────────────


@pytest.mark.parametrize("bad", [[], ["not-a-uuid"], [""], [None], [123]])
def test_invalid_input_raises_before_any_effect(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog, bad: list[Any]
) -> None:
    _seed(store)
    with pytest.raises(ValueError):
        delete_runs(store, TENANT, bad, capsule_dir=capsule_dir, actor="a", audit_log=audit)
    assert _rows(store, "runs") == 1 and _audit_events(audit) == []


def test_over_ceiling_is_rejected(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    ids = [uuid4() for _ in range(MAX_RUNS_PER_REQUEST + 1)]
    with pytest.raises(ValueError, match="ceiling"):
        delete_runs(store, TENANT, ids, capsule_dir=capsule_dir, actor="a", audit_log=audit)


def test_one_bad_id_rejects_the_whole_request(
    store: SQLiteMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    (rid,) = _seed(store)
    with pytest.raises(ValueError, match="not a UUID"):
        delete_runs(store, TENANT, [rid, "nope"], capsule_dir=capsule_dir, actor="a",
                    audit_log=audit)
    assert store.lookup_run(rid, TENANT) is not None


def test_store_without_delete_run_raises(capsule_dir: Path, audit: AuditLog) -> None:
    from contextlib import nullcontext

    class _NoDelete:
        def begin_tenant_context(self, tenant_id: UUID) -> Any:
            return nullcontext(self)

        def delete_run(self, run_id: UUID, tenant_id: UUID) -> int:
            raise NotImplementedError("no delete_run")

    with pytest.raises(NotImplementedError):
        delete_runs(_NoDelete(), TENANT, [uuid4()], capsule_dir=capsule_dir,  # type: ignore[arg-type]
                    actor="a", audit_log=audit)


# ── Postgres: SQL shape (no database) ───────────────────────────────────────


class _Cur:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _RecConn:
    def __init__(self, runs_removed: int) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._runs_removed = runs_removed

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _Cur:
        self.calls.append((sql, params))
        return _Cur(self._runs_removed if sql.startswith("DELETE FROM runs") else 0)


def test_postgres_delete_run_issues_three_tenant_scoped_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _RecConn(runs_removed=1)
    pg = PostgresMetadataStore(dsn="postgresql://unused")
    monkeypatch.setattr(pg, "_conn", lambda: conn)
    rid = uuid4()
    assert pg.delete_run(rid, TENANT) == 1
    tables = [c[0].split()[2] for c in conn.calls]
    assert tables == ["runs", "capsules", "signatures"]
    for sql, params in conn.calls:
        assert "tenant_id = %s" in sql and "run_id = %s" in sql
        assert params == (str(rid), str(TENANT))


def test_postgres_delete_run_requires_tenant_context() -> None:
    from novafabric.metadata_store.interface import RLSContextMissing

    pg = PostgresMetadataStore(dsn="postgresql://unused")
    with pytest.raises(RLSContextMissing):
        pg.delete_run(uuid4(), TENANT)


# ── Postgres: real database (container tier; skips without a DSN) ───────────


@pytest.fixture()
def pg_store(postgres_url: str) -> Iterator[PostgresMetadataStore]:
    s = PostgresMetadataStore(dsn=postgres_url)
    s.bootstrap()
    yield s


def test_pg_delete_run_roundtrip(pg_store: PostgresMetadataStore) -> None:
    keep, gone = uuid4(), uuid4()
    with pg_store.begin_tenant_context(TENANT) as ctx:
        for rid in (keep, gone):
            ctx.register_run(rid, TENANT, event_type="run.started")
            ctx.record_signature(rid, TENANT, f"sig-{rid}", {"k": "v"})
    with pg_store.begin_tenant_context(TENANT) as ctx:
        assert ctx.delete_run(gone, TENANT) == 1
        assert ctx.delete_run(gone, TENANT) == 0
    with pg_store.begin_tenant_context(TENANT) as ctx:
        assert ctx.lookup_run(gone, TENANT) is None
        assert ctx.lookup_run(keep, TENANT) is not None


def test_pg_delete_run_does_not_cross_tenants(pg_store: PostgresMetadataStore) -> None:
    rid = uuid4()
    with pg_store.begin_tenant_context(TENANT) as ctx:
        ctx.register_run(rid, TENANT, event_type="run.started")
    with pg_store.begin_tenant_context(OTHER_TENANT) as ctx:
        assert ctx.delete_run(rid, TENANT) == 0  # RLS hides it from the other tenant
    with pg_store.begin_tenant_context(TENANT) as ctx:
        assert ctx.lookup_run(rid, TENANT) is not None


def test_pg_governed_refusal_deletes_nothing(
    pg_store: PostgresMetadataStore, capsule_dir: Path, audit: AuditLog
) -> None:
    ids = [uuid4(), uuid4()]
    with pg_store.begin_tenant_context(TENANT) as ctx:
        for rid in ids:
            ctx.register_run(rid, TENANT, event_type="run.started")
    _place_hold(capsule_dir)
    with pytest.raises(RunDeleteRefusedError):
        delete_runs(pg_store, TENANT, ids, capsule_dir=capsule_dir, actor="a", audit_log=audit)
    with pg_store.begin_tenant_context(TENANT) as ctx:
        assert all(ctx.lookup_run(i, TENANT) is not None for i in ids)
