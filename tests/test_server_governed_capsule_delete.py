"""ADR-0206 P2 wiring — capsule delete and index delete go through one governed path.

Covers ``DELETE /v0/capsules/{id}``, ``POST /v0/capsules/bulk-delete`` and serve's
``DELETE /api/runs/{id}``: success removes capsule + runs-cache + MetadataStore rows;
holds / WORM / seal refuse (nothing deleted, ``run.index_delete_refused`` audited);
a missing run is a 404; a failure midway restores the capsule so index and files
agree; a purge failure leaves hidden residue, never a visible capsule.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.metadata_store.sqlite import SQLiteMetadataStore  # noqa: E402
from novafabric.server import capsule_delete, capsule_index  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import AuthContext, verify_token  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402
from novafabric.storage._local_worm import LocalWormAdapter  # noqa: E402

TENANT = UUID("11111111-1111-4111-8111-111111111111")


@dataclass
class _TenantAuth(AuthContext):
    tenant_id: str | None = None


def _write_capsule(capsule_dir: Path, run_id: str) -> Path:
    cdir = capsule_dir / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "capsule.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "0.1.0",
                "run_id": run_id,
                "status": "success",
                "created_at": "2026-04-15T10:00:00+00:00",
            }
        )
    )
    (cdir / "trace.jsonl").write_text("")
    return cdir


def _add_hold(capsule_dir: Path) -> None:
    reg = capsule_dir.parent / "registries" / "legal"
    reg.mkdir(parents=True, exist_ok=True)
    (reg / "holds.jsonl").write_text(
        json.dumps({"hold_id": "HOLD-1", "released_at": None}) + "\n"
    )


def _audit_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _event_types(path: Path) -> list[str]:
    return [e["event_type"] for e in _audit_events(path)]


def _cache_count(db_path: Path, run_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM runs_cache WHERE run_id=?", (run_id,)).fetchone()[0]
        )
    finally:
        conn.close()


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "capsules"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hash-chained audit log path used by the governed path."""
    p = tmp_path / "chain-audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(p))
    return p


@pytest.fixture
def store(tmp_path: Path) -> SQLiteMetadataStore:
    with pytest.warns(UserWarning):
        s = SQLiteMetadataStore(db_path=tmp_path / "meta.db")
    s.bootstrap()
    return s


@pytest.fixture
def client(
    db_path: Path,
    capsule_dir: Path,
    store: SQLiteMetadataStore,
    chain: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    from novafabric.server import deps
    from novafabric.server.routes import capsules as capsules_routes

    # The route resolves the store like the upload path does (a direct call).
    monkeypatch.setattr(capsules_routes, "get_metadata_store_dep", lambda: store)

    app = create_app(ServerConfig(db_path=str(db_path), insecure_no_auth=True))
    app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
    app.dependency_overrides[verify_token] = lambda: _TenantAuth(
        subject="admin@example.com", roles=["admin"], tenant_id=str(TENANT)
    )
    return TestClient(app, raise_server_exceptions=False)


def _seed(client: TestClient, store: SQLiteMetadataStore, capsule_dir: Path) -> str:
    """A capsule with a runs-cache row and a MetadataStore row."""
    rid = str(uuid4())
    _write_capsule(capsule_dir, rid)
    assert client.get("/v0/capsules").status_code == 200  # populate runs-cache
    store.register_run(UUID(rid), TENANT, event_type="run.started")
    return rid


def _store_rows(store: SQLiteMetadataStore) -> int:
    with sqlite3.connect(store._db_path) as c:
        return int(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0])


# ----------------------------------------------------------------------- v0 single


def test_delete_removes_capsule_cache_and_metadata_rows_and_audits(
    client: TestClient, store: SQLiteMetadataStore, capsule_dir: Path, db_path: Path, chain: Path
) -> None:
    rid = _seed(client, store, capsule_dir)
    assert _cache_count(db_path, rid) == 1 and _store_rows(store) == 1

    resp = client.delete(f"/v0/capsules/{rid}")

    assert resp.status_code == 200, resp.text
    assert not (capsule_dir / rid).exists()
    assert _cache_count(db_path, rid) == 0
    assert _store_rows(store) == 0
    ev = [e for e in _audit_events(chain) if e["event_type"] == "run.index_delete"]
    assert len(ev) == 1 and ev[0]["resource_id"] == rid
    assert ev[0]["details"]["scope"] == "metadata_index"
    # the tombstone area is left empty on success
    assert list((capsule_dir.parent / ".deleting").iterdir()) == []


def test_held_refuses_deletes_nothing_and_audits_refusal(
    client: TestClient, store: SQLiteMetadataStore, capsule_dir: Path, db_path: Path, chain: Path
) -> None:
    rid = _seed(client, store, capsule_dir)
    _add_hold(capsule_dir)

    resp = client.delete(f"/v0/capsules/{rid}")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "legal_hold_active"
    assert (capsule_dir / rid / "capsule.yaml").exists()
    assert _cache_count(db_path, rid) == 1 and _store_rows(store) == 1
    assert _event_types(chain) == ["run.index_delete_refused"]
    ev = _audit_events(chain)[0]
    assert ev["details"]["refusals"][0]["code"] == "legal_hold_active"


def test_sealed_capsule_refused_by_default(
    client: TestClient, store: SQLiteMetadataStore, capsule_dir: Path, db_path: Path, chain: Path
) -> None:
    rid = _seed(client, store, capsule_dir)
    (capsule_dir / rid / ".seal").mkdir()

    resp = client.delete(f"/v0/capsules/{rid}")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "sealed_capsule"
    assert (capsule_dir / rid / ".seal").is_dir()
    assert _cache_count(db_path, rid) == 1 and _store_rows(store) == 1
    assert _event_types(chain) == ["run.index_delete_refused"]


def test_sealed_capsule_deletable_when_policy_explicitly_allows(
    client: TestClient,
    store: SQLiteMetadataStore,
    capsule_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = _seed(client, store, capsule_dir)
    (capsule_dir / rid / ".seal").mkdir()
    monkeypatch.setenv(capsule_delete.ALLOW_SEALED_ENV, "1")

    assert client.delete(f"/v0/capsules/{rid}").status_code == 200
    assert not (capsule_dir / rid).exists() and _store_rows(store) == 0


def test_hold_beats_allow_sealed(
    capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capsule(capsule_dir, "r1")
    (capsule_dir / "r1" / ".seal").mkdir()
    _add_hold(capsule_dir)
    monkeypatch.setenv(capsule_delete.ALLOW_SEALED_ENV, "1")
    with pytest.raises(capsule_delete.DeleteBlockedError) as ei:
        capsule_delete.check_deletable(capsule_dir, "r1")
    assert ei.value.code == "legal_hold_active"


def test_worm_locked_refused(
    client: TestClient, store: SQLiteMetadataStore, capsule_dir: Path, chain: Path
) -> None:
    rid = _seed(client, store, capsule_dir)
    reg = capsule_dir.parent / "registries" / "default"
    reg.mkdir(parents=True)
    LocalWormAdapter(reg / "worm.db").put(rid, b"x", retention_days=30)

    resp = client.delete(f"/v0/capsules/{rid}")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "worm_hold"
    assert (capsule_dir / rid).is_dir() and _store_rows(store) == 1
    assert _event_types(chain) == ["run.index_delete_refused"]


def test_missing_run_404_nothing_audited(client: TestClient, chain: Path) -> None:
    assert client.delete(f"/v0/capsules/{uuid4()}").status_code == 404
    assert _audit_events(chain) == []


def test_index_failure_midway_restores_capsule_and_cache(
    client: TestClient,
    store: SQLiteMetadataStore,
    capsule_dir: Path,
    db_path: Path,
    chain: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rid = _seed(client, store, capsule_dir)

    def boom(self: Any, run_id: UUID, tenant_id: UUID) -> int:
        raise RuntimeError("metadata store down")

    with monkeypatch.context() as m:
        m.setattr(SQLiteMetadataStore, "delete_run", boom)
        resp = client.delete(f"/v0/capsules/{rid}")

    assert resp.status_code == 500
    # capsule is back, runs-cache re-healed, store row untouched: all agree
    assert (capsule_dir / rid / "capsule.yaml").exists()
    assert _cache_count(db_path, rid) == 1
    assert _store_rows(store) == 1
    assert "run.index_delete" not in _event_types(chain)
    # and a retry once the store is healthy succeeds
    assert client.delete(f"/v0/capsules/{rid}").status_code == 200
    assert _store_rows(store) == 0


def test_failure_is_audited_in_serve_audit(
    client: TestClient,
    store: SQLiteMetadataStore,
    capsule_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novafabric._paths import dashboard_audit_path

    rid = _seed(client, store, capsule_dir)
    monkeypatch.setattr(
        SQLiteMetadataStore,
        "delete_run",
        lambda self, a, b: (_ for _ in ()).throw(RuntimeError("x")),
    )
    assert client.delete(f"/v0/capsules/{rid}").status_code == 500
    entries = [
        json.loads(x) for x in dashboard_audit_path().read_text().splitlines() if x.strip()
    ]
    failed = [e for e in entries if e["action"] == "capsule_delete_failed"]
    assert len(failed) == 1 and failed[0]["args"]["code"] == "delete_failed"


# ----------------------------------------------------------------------- v0 bulk


def test_bulk_hold_refuses_every_item_and_deletes_nothing(
    client: TestClient, store: SQLiteMetadataStore, capsule_dir: Path, chain: Path
) -> None:
    ids = [_seed(client, store, capsule_dir) for _ in range(2)]
    _add_hold(capsule_dir)

    resp = client.post("/v0/capsules/bulk-delete", json={"run_ids": ids})

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["held"] == 2 and body["summary"]["deleted"] == 0
    assert all((capsule_dir / i / "capsule.yaml").exists() for i in ids)
    assert _store_rows(store) == 2
    refused = [e for e in _audit_events(chain) if e["event_type"] == "run.index_delete_refused"]
    assert len(refused) == 1 and len(refused[0]["details"]["refusals"]) == 2


def test_bulk_mixed_sealed_item_held_others_deleted(
    client: TestClient, store: SQLiteMetadataStore, capsule_dir: Path, chain: Path
) -> None:
    a, b = _seed(client, store, capsule_dir), _seed(client, store, capsule_dir)
    (capsule_dir / b / ".seal").mkdir()

    resp = client.post("/v0/capsules/bulk-delete", json={"run_ids": [a, b]})

    out = {r["run_id"]: r for r in resp.json()["results"]}
    assert out[a]["outcome"] == "deleted"
    assert out[b] == {"run_id": b, "outcome": "held", "code": "sealed_capsule"}
    assert not (capsule_dir / a).exists() and (capsule_dir / b).exists()
    assert _store_rows(store) == 1
    types = _event_types(chain)
    assert types.count("run.index_delete") == 1
    assert types.count("run.index_delete_refused") == 1


def test_bulk_partial_failure_reports_item_error_and_restores_it(
    client: TestClient,
    store: SQLiteMetadataStore,
    capsule_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = _seed(client, store, capsule_dir), _seed(client, store, capsule_dir)
    real = SQLiteMetadataStore.delete_run

    def flaky(self: Any, run_id: UUID, tenant_id: UUID) -> int:
        if str(run_id) == b:
            raise RuntimeError("boom")
        return real(self, run_id, tenant_id)

    monkeypatch.setattr(SQLiteMetadataStore, "delete_run", flaky)
    resp = client.post("/v0/capsules/bulk-delete", json={"run_ids": [a, b]})

    body = resp.json()
    out = {r["run_id"]: r for r in body["results"]}
    assert out[a]["outcome"] == "deleted"
    assert out[b]["outcome"] == "error" and out[b]["code"] == "delete_failed"
    assert body["summary"]["errors"] == 1
    assert (capsule_dir / b / "capsule.yaml").exists() and not (capsule_dir / a).exists()
    assert _store_rows(store) == 1  # b's row and capsule both survive


# ------------------------------------------------------------ core ordering/recovery


def test_rollback_failure_is_inconsistent_and_names_tombstone(
    capsule_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capsule(capsule_dir, "r1")
    conn = capsule_index.open_index(db_path)
    real_rename = os.rename
    calls = {"n": 0}

    def rename(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if Path(dst) == capsule_dir / "r1":  # the rename-back
            raise OSError("disk gone")
        real_rename(src, dst)

    def bad_remove(c: Any, run_id: str) -> None:
        raise RuntimeError("index down")

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(capsule_index, "remove_run", bad_remove)
    with pytest.raises(capsule_delete.DeleteFailedError) as ei:
        capsule_delete.execute_delete(capsule_dir, "r1", conn)
    conn.close()
    assert ei.value.code == "delete_inconsistent"
    tomb = Path(str(ei.value.details["tombstone"]))
    assert (tomb / "capsule.yaml").exists()  # bytes preserved for an operator
    assert not (capsule_dir / "r1").exists()


def _make_inconsistent(
    capsule_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str = "r1"
) -> capsule_delete.DeleteFailedError:
    """Drive a delete whose index step AND rollback both fail."""
    conn = capsule_index.open_index(db_path)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(dst) == capsule_dir / run_id:
            raise OSError("disk gone")
        real_rename(src, dst)

    def bad_remove(c: Any, rid: str) -> None:
        raise RuntimeError("index down")

    with monkeypatch.context() as m:
        m.setattr(os, "rename", rename)
        m.setattr(capsule_index, "remove_run", bad_remove)
        with pytest.raises(capsule_delete.DeleteFailedError) as ei:
            capsule_delete.execute_delete(capsule_dir, run_id, conn)
    conn.close()
    return ei.value


def test_inconsistent_tombstone_is_never_reaped(
    capsule_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tombstone is the capsule's ONLY copy: age must never make it reapable."""
    _write_capsule(capsule_dir, "r1")
    err = _make_inconsistent(capsule_dir, db_path, monkeypatch)
    tomb = Path(str(err.details["tombstone"]))
    far = time.time() + 365 * 86400
    capsule_delete.reap_tombstones(capsule_dir, now=far)
    assert (tomb / "capsule.yaml").exists(), "reaper destroyed the only copy"
    assert tomb.name.endswith(".inconsistent")
    assert str(tomb) in str(err)  # operator text names the FINAL path
    # a later delete of ANOTHER capsule (which reaps first) leaves it too
    _write_capsule(capsule_dir, "r2")
    capsule_delete.execute_delete(capsule_dir, "r2", None)
    assert (tomb / "capsule.yaml").exists()


def test_reaper_still_reaps_old_orphans_but_spares_inconsistent_beside_them(
    capsule_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capsule(capsule_dir, "r1")
    err = _make_inconsistent(capsule_dir, db_path, monkeypatch)
    tomb = Path(str(err.details["tombstone"]))
    base = tomb.parent
    old = base / f"orphan.{int(time.time()) - 7200}.deadbeef"
    young = base / f"orphan2.{int(time.time())}.deadbeef"
    old.mkdir()
    young.mkdir()
    assert capsule_delete.reap_tombstones(capsule_dir) == 1  # only the old orphan
    assert not old.exists() and young.is_dir() and tomb.is_dir()


def test_retry_while_inconsistent_tombstone_exists_is_refused(
    capsule_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capsule(capsule_dir, "r1")
    err = _make_inconsistent(capsule_dir, db_path, monkeypatch)
    tomb = Path(str(err.details["tombstone"]))
    # a visible copy reappears (e.g. partially restored); a retry must not
    # create a second tombstone or mask the first
    _write_capsule(capsule_dir, "r1")
    conn = capsule_index.open_index(db_path)
    with pytest.raises(capsule_delete.DeleteFailedError) as ei:
        capsule_delete.execute_delete(capsule_dir, "r1", conn)
    conn.close()
    assert ei.value.code == "delete_inconsistent_pending"
    assert str(tomb) in str(ei.value)
    assert (capsule_dir / "r1" / "capsule.yaml").exists()
    assert sorted(p.name for p in tomb.parent.iterdir()) == [tomb.name]


def test_inconsistent_http_audit_names_final_path_and_retry_is_not_404(
    client: TestClient,
    store: SQLiteMetadataStore,
    capsule_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from novafabric._paths import dashboard_audit_path

    rid = _seed(client, store, capsule_dir)
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(dst) == capsule_dir / rid:
            raise OSError("disk gone")
        real_rename(src, dst)

    with monkeypatch.context() as m:
        m.setattr(os, "rename", rename)
        m.setattr(
            SQLiteMetadataStore,
            "delete_run",
            lambda self, a, b: (_ for _ in ()).throw(RuntimeError("x")),
        )
        resp = client.delete(f"/v0/capsules/{rid}")
    assert resp.status_code == 500
    entries = [
        json.loads(x) for x in dashboard_audit_path().read_text().splitlines() if x.strip()
    ]
    failed = [e for e in entries if e["action"] == "capsule_delete_failed"]
    assert failed[0]["args"]["code"] == "delete_inconsistent"
    tomb = Path(failed[0]["args"]["tombstone"])
    assert tomb.name.endswith(".inconsistent")
    assert (tomb / "capsule.yaml").exists()
    # retry: not a bare 404 that hides the stranded bytes
    again = client.delete(f"/v0/capsules/{rid}")
    assert again.status_code == 409
    assert str(tomb) in again.text


def test_tombstone_rename_failure_changes_nothing(
    capsule_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capsule(capsule_dir, "r1")
    conn = capsule_index.open_index(db_path)

    def rename(src: Any, dst: Any) -> None:
        raise OSError("EXDEV")

    monkeypatch.setattr(os, "rename", rename)
    with pytest.raises(capsule_delete.DeleteFailedError) as ei:
        capsule_delete.execute_delete(capsule_dir, "r1", conn)
    conn.close()
    assert ei.value.code == "delete_failed" and ei.value.details["stage"] == "tombstone"
    assert (capsule_dir / "r1" / "capsule.yaml").exists()


def test_purge_failure_leaves_hidden_residue_then_reaped(
    capsule_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capsule(capsule_dir, "r1")
    conn = capsule_index.open_index(db_path)

    def nope(path: Any, *a: Any, **k: Any) -> None:
        raise OSError("busy")

    monkeypatch.setattr(capsule_delete.shutil, "rmtree", nope)
    out = capsule_delete.execute_delete(capsule_dir, "r1", conn)
    conn.close()
    monkeypatch.undo()

    assert out.residue is not None and Path(out.residue).is_dir()
    assert not (capsule_dir / "r1").exists()  # never visible again
    # a young tombstone is left (a sibling might still roll back) ...
    assert capsule_delete.reap_tombstones(capsule_dir) == 0
    # ... an old one is reaped
    far = time.time() + capsule_delete.TOMBSTONE_REAP_AFTER_S + 10
    assert capsule_delete.reap_tombstones(capsule_dir, now=far) == 1
    assert not Path(out.residue).exists()


def test_reap_ignores_foreign_names(capsule_dir: Path) -> None:
    base = capsule_dir.parent / capsule_delete.TOMBSTONE_DIRNAME
    base.mkdir()
    (base / "not-a-tombstone").mkdir()
    assert capsule_delete.reap_tombstones(capsule_dir, now=time.time() + 1e9) == 0
    assert capsule_delete.reap_tombstones(capsule_dir / "x" / "y") == 0


def test_non_uuid_run_id_skips_store(
    capsule_dir: Path, store: SQLiteMetadataStore, chain: Path
) -> None:
    _write_capsule(capsule_dir, "01ULIDSTYLE000000000000")
    out = capsule_delete.execute_delete(
        capsule_dir, "01ULIDSTYLE000000000000", None, store=store, tenant_id=TENANT
    )
    assert out.metadata_rows_removed == 0
    assert _audit_events(chain) == []


def test_audit_failure_never_undoes_delete(
    capsule_dir: Path, store: SQLiteMetadataStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = uuid4()
    _write_capsule(capsule_dir, str(rid))
    store.register_run(rid, TENANT, event_type="run.started")

    class Broken:
        def append(self, **kw: Any) -> None:
            raise OSError("log read-only")

    out = capsule_delete.execute_delete(
        capsule_dir, str(rid), None, store=store, tenant_id=TENANT, audit_log=Broken()  # type: ignore[arg-type]
    )
    assert out.metadata_rows_removed == 1 and not (capsule_dir / str(rid)).exists()


# ------------------------------------------------------------------------- serve


VALID_TOKEN = "test-token-governed-del"
H = {"host": "127.0.0.1:4321"}


def _serve_client(tmp_path: Path, capsule_dir: Path) -> TestClient:
    from novafabric.serve.app import create_app as create_serve

    app = create_serve(
        capsule_dir=capsule_dir,
        token=VALID_TOKEN,
        db_path=tmp_path / "serve-registry.db",
        static_dir=None,
    )
    return TestClient(app)


def test_serve_delete_removes_runs_cache_row(
    tmp_path: Path, capsule_dir: Path, chain: Path
) -> None:
    _write_capsule(capsule_dir, "srv1")
    db = tmp_path / "serve-registry.db"
    conn = capsule_index.open_index(db)
    capsule_index.upsert_capsule(conn, capsule_dir, "srv1", {"status": "success"})
    conn.close()
    with _serve_client(tmp_path, capsule_dir) as tc:
        r = tc.delete("/api/runs/srv1", params={"token": VALID_TOKEN}, headers=H)
    assert r.status_code == 200, r.text
    assert not (capsule_dir / "srv1").exists()
    assert _cache_count(db, "srv1") == 0


def test_serve_delete_refuses_sealed_and_worm(
    tmp_path: Path, capsule_dir: Path, chain: Path
) -> None:
    _write_capsule(capsule_dir, "sealed1")
    (capsule_dir / "sealed1" / ".seal").mkdir()
    _write_capsule(capsule_dir, "worm1")
    reg = capsule_dir.parent / "registries" / "default"
    reg.mkdir(parents=True)
    LocalWormAdapter(reg / "worm.db").put("worm1", b"x", retention_days=30)
    with _serve_client(tmp_path, capsule_dir) as tc:
        for rid in ("sealed1", "worm1"):
            r = tc.delete(
                f"/api/runs/{rid}", params={"token": VALID_TOKEN, "force": "true"}, headers=H
            )
            assert r.status_code == 409, r.text
            assert (capsule_dir / rid).is_dir()
    assert _event_types(chain) == ["run.index_delete_refused"] * 2


# ---- residual gaps (ADR-0206 P2): plain tombstone safety + serve 409 ----


def _plain_tombstone(capsule_dir: Path, run_id: str, *, age_s: float = 7200) -> Path:
    """A plain (reapable-by-name) tombstone: marking AND rollback both failed."""
    base = capsule_dir.parent / capsule_delete.TOMBSTONE_DIRNAME
    base.mkdir(exist_ok=True)
    tomb = base / f"{run_id}.{int(time.time() - age_s)}.cafef00d"
    tomb.mkdir()
    (tomb / "capsule.yaml").write_text("run_id: x\n")
    return tomb


def _write_audit(path: Path, *entries: tuple[str, str]) -> None:
    path.write_text(
        "".join(json.dumps({"action": a, "args": {"run_id": r}}) + "\n" for a, r in entries)
    )


def test_double_rename_failure_end_to_end_plain_tombstone_survives_reaper(
    capsule_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capsule(capsule_dir, "r1")
    conn = capsule_index.open_index(db_path)
    capsule_index.upsert_capsule(conn, capsule_dir, "r1", {"status": "success"})
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if str(dst).endswith(capsule_delete.INCONSISTENT_SUFFIX) or Path(dst) == capsule_dir / "r1":
            raise OSError("disk gone")
        real_rename(src, dst)

    def bad_remove(c: Any, rid: str) -> None:
        raise RuntimeError("index down")

    with monkeypatch.context() as m:
        m.setattr(os, "rename", rename)
        m.setattr(capsule_index, "remove_run", bad_remove)
        with pytest.raises(capsule_delete.DeleteFailedError) as ei:
            capsule_delete.execute_delete(capsule_dir, "r1", conn)
    tomb = Path(str(ei.value.details["tombstone"]))
    assert not tomb.name.endswith(".inconsistent")
    far = time.time() + 365 * 86400
    assert capsule_delete.reap_tombstones(capsule_dir, now=far, conn=conn) == 0
    conn.close()
    assert (tomb / "capsule.yaml").exists(), "reaper destroyed the only copy"


def test_reaper_keeps_old_tombstone_with_runs_cache_row(capsule_dir: Path, db_path: Path) -> None:
    _write_capsule(capsule_dir, "r1")
    conn = capsule_index.open_index(db_path)
    capsule_index.upsert_capsule(conn, capsule_dir, "r1", {"status": "success"})
    tomb = _plain_tombstone(capsule_dir, "r1")
    assert capsule_delete.reap_tombstones(capsule_dir, conn=conn) == 0
    assert tomb.is_dir()
    capsule_index.remove_run(conn, "r1")  # row gone: now a genuine orphan
    conn.commit()
    assert capsule_delete.reap_tombstones(capsule_dir, conn=conn) == 1
    conn.close()
    assert not tomb.exists()


def test_reaper_keeps_old_tombstone_with_metadata_store_row(
    capsule_dir: Path, store: SQLiteMetadataStore
) -> None:
    rid = str(uuid4())
    store.register_run(UUID(rid), TENANT, event_type="run.started")
    tomb = _plain_tombstone(capsule_dir, rid)
    assert capsule_delete.reap_tombstones(capsule_dir, store=store, tenant_id=TENANT) == 0
    assert tomb.is_dir()
    store.delete_run(UUID(rid), TENANT)
    assert capsule_delete.reap_tombstones(capsule_dir, store=store, tenant_id=TENANT) == 1


def test_reaper_keeps_tombstone_with_unfinished_delete_audit_entry(
    capsule_dir: Path, tmp_path: Path
) -> None:
    audit = tmp_path / "audit.jsonl"
    tomb = _plain_tombstone(capsule_dir, "r1")
    done = _plain_tombstone(capsule_dir, "r2")
    _write_audit(
        audit,
        ("capsule_delete_failed", "r1"),  # never finished
        ("capsule_delete_failed", "r2"),
        ("capsule_delete", "r2"),  # failed, then retried successfully
    )
    assert capsule_delete.reap_tombstones(capsule_dir, audit_path=audit) == 1
    assert tomb.is_dir() and not done.exists()


def test_reaper_fails_safe_on_unreadable_index_or_audit(
    capsule_dir: Path, db_path: Path, tmp_path: Path
) -> None:
    tomb = _plain_tombstone(capsule_dir, "r1")
    conn = capsule_index.open_index(db_path)
    conn.close()  # every query now raises
    assert capsule_delete.reap_tombstones(capsule_dir, conn=conn) == 0
    assert tomb.is_dir()
    bad = tmp_path / "bad-audit.jsonl"
    bad.write_text("{not json\n")
    assert capsule_delete.reap_tombstones(capsule_dir, audit_path=bad) == 0
    assert tomb.is_dir()


def test_reaper_leaves_young_tombstone_untouched(capsule_dir: Path, db_path: Path) -> None:
    tomb = _plain_tombstone(capsule_dir, "r1", age_s=5)
    conn = capsule_index.open_index(db_path)
    assert capsule_delete.reap_tombstones(capsule_dir, conn=conn) == 0
    conn.close()
    assert tomb.is_dir()


def test_serve_retry_with_inconsistent_tombstone_is_409_not_404(
    tmp_path: Path, capsule_dir: Path, chain: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_capsule(capsule_dir, "srvi")
    real_rename = os.rename

    def rename(src: Any, dst: Any) -> None:
        if Path(dst) == capsule_dir / "srvi":
            raise OSError("disk gone")
        real_rename(src, dst)

    def bad_remove(c: Any, rid: str) -> None:
        raise RuntimeError("index down")

    with _serve_client(tmp_path, capsule_dir) as tc:
        with monkeypatch.context() as m:
            m.setattr(os, "rename", rename)
            m.setattr(capsule_index, "remove_run", bad_remove)
            first = tc.delete("/api/runs/srvi", params={"token": VALID_TOKEN}, headers=H)
        assert first.status_code == 500, first.text
        tomb = capsule_delete.inconsistent_tombstones(capsule_dir, "srvi")[0]
        again = tc.delete("/api/runs/srvi", params={"token": VALID_TOKEN}, headers=H)
        assert again.status_code == 409, again.text
        assert "delete_inconsistent_pending" in again.text and str(tomb) in again.text
        missing = tc.delete("/api/runs/nope", params={"token": VALID_TOKEN}, headers=H)
        assert missing.status_code == 404
