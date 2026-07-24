"""ADR-0206 P1 — bulk capsule ops + keyset pagination (experimental).

Covers, against the ``/v0`` server surface:

- v1 keyset cursor round-trip and strict decode (tamper/garbage/unknown
  version → 400 ``invalid_cursor``, replacing silent restart-at-zero);
- keyset pagination over a seeded store: stable ``created_at DESC, run_id
  DESC`` order, exactly-once iteration, no skips/dupes when rows are
  deleted/inserted across a page boundary (the offset failure mode);
- legacy ``{"offset": N}`` cursors still served, with the ADR-0188
  ``Deprecation: true`` response header, and refusable by config;
- the O(page) guard: steady-state page requests parse zero manifests;
- ``DELETE /v0/capsules/{run_id}``: admin-only, hold/WORM refusal, derived
  index cleanup (runs_cache + content index), audit evidence, 404/400 edges;
- ``POST /v0/capsules/bulk-delete``: per-item outcomes, batch cap 422,
  dry-run, duplicate/invalid handling, audit summary entries.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric._paths import dashboard_audit_path  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import AuthContext, verify_token  # noqa: E402
from novafabric.server.config import BulkConfig, ServerConfig  # noqa: E402
from novafabric.server.pagination import (  # noqa: E402
    InvalidCursorError,
    encode_cursor,
    encode_keyset_cursor,
    parse_cursor,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _write_capsule(
    capsule_dir: Path,
    run_id: str,
    created_at: str | None = "2026-04-15T10:00:00+00:00",
) -> Path:
    manifest: dict[str, Any] = {
        "schema_version": "0.1.0",
        "novafabric_version": "0.6.12",
        "run_id": run_id,
        "finished_at": "2026-04-15T10:00:01+00:00",
        "duration_ms": 1000,
        "command": ["python", "-c", "print('hi')"],
        "exit_code": 0,
        "status": "success",
    }
    if created_at is not None:
        manifest["created_at"] = created_at
    cdir = capsule_dir / run_id
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cdir / "trace.jsonl").write_text("")
    return cdir


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    cdir = tmp_path / "capsules"
    cdir.mkdir()
    return cdir


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture
def cfg(db_path: Path) -> ServerConfig:
    return ServerConfig(db_path=str(db_path), insecure_no_auth=True)


def _make_client(cfg: ServerConfig, capsule_dir: Path) -> TestClient:
    from novafabric.server import deps

    app = create_app(cfg)
    app.dependency_overrides[deps.get_capsule_dir] = lambda: capsule_dir
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(cfg: ServerConfig, capsule_dir: Path) -> TestClient:
    return _make_client(cfg, capsule_dir)


def _client_with_role(
    cfg: ServerConfig, capsule_dir: Path, roles: list[str]
) -> TestClient:
    """Client whose token resolves to *roles* (verify_token override)."""
    c = _make_client(cfg, capsule_dir)
    c.app.dependency_overrides[verify_token] = lambda: AuthContext(  # type: ignore[attr-defined]
        subject="role-test@example.com", roles=roles
    )
    return c


def _audit_entries(action: str | None = None) -> list[dict[str, Any]]:
    p = dashboard_audit_path()
    if not p.exists():
        return []
    entries = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    if action is not None:
        entries = [e for e in entries if e["action"] == action]
    return entries


def _walk_pages(client: TestClient, limit: int) -> tuple[list[str], list[dict[str, Any]]]:
    """Follow next_cursor to exhaustion; return (run_ids in order, raw pages)."""
    ids: list[str] = []
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(50):  # bounded walk
        url = f"/v0/capsules?limit={limit}"
        if cursor:
            url += f"&cursor={cursor}"
        resp = client.get(url)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        pages.append(body)
        ids.extend(item["run_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return ids, pages
    raise AssertionError("pagination did not terminate")


# --------------------------------------------------------------------------- #
# Cursor unit tests (strict decode, ADR-0206 D1)
# --------------------------------------------------------------------------- #


class TestCursorParsing:
    def test_keyset_roundtrip(self) -> None:
        cur = encode_keyset_cursor("2026-04-15T10:00:00+00:00", "run-a")
        parsed = parse_cursor(cur)
        assert parsed.kind == "keyset"
        assert parsed.key == ("2026-04-15T10:00:00+00:00", "run-a")

    def test_keyset_roundtrip_null_created_at(self) -> None:
        parsed = parse_cursor(encode_keyset_cursor(None, "run-b"))
        assert parsed.kind == "keyset"
        assert parsed.key == (None, "run-b")

    def test_absent_and_empty_are_first_page(self) -> None:
        assert parse_cursor(None).kind == "first"
        assert parse_cursor("").kind == "first"

    def test_legacy_offset_cursor_parses(self) -> None:
        parsed = parse_cursor(encode_cursor(7))
        assert parsed.kind == "offset"
        assert parsed.offset == 7

    @pytest.mark.parametrize(
        "cursor",
        [
            "not-base64!!!",
            "eyJ2IjoyLCJrIjpbImEiLCJiIl19",  # {"v":2,...} unknown version
            "WyJhIiwiYiJd",  # JSON list, not object
            "e30",  # {} — neither v nor offset
        ],
    )
    def test_garbage_and_unknown_versions_raise(self, cursor: str) -> None:
        with pytest.raises(InvalidCursorError):
            parse_cursor(cursor)

    @pytest.mark.parametrize(
        "payload",
        [
            {"v": 1},  # missing k
            {"v": 1, "k": ["a"]},  # wrong arity
            {"v": 1, "k": ["a", 5]},  # id not a str
            {"v": 1, "k": [3, "b"]},  # created_at not str/null
            {"v": 1, "k": ["a", ""]},  # empty id
            {"offset": -1},
            {"offset": "x"},
        ],
    )
    def test_malformed_payloads_raise(self, payload: dict[str, Any]) -> None:
        import base64

        cur = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        with pytest.raises(InvalidCursorError):
            parse_cursor(cur)


# --------------------------------------------------------------------------- #
# Keyset pagination over the API
# --------------------------------------------------------------------------- #


def _seed(capsule_dir: Path, n: int) -> list[str]:
    """n capsules with strictly increasing created_at; returns ids newest-first."""
    ids = []
    for i in range(n):
        rid = f"01SEED{i:020d}"
        _write_capsule(capsule_dir, rid, created_at=f"2026-04-15T10:{i:02d}:00+00:00")
        ids.append(rid)
    return list(reversed(ids))  # created_at DESC


class TestKeysetPagination:
    def test_walk_yields_each_capsule_exactly_once_in_order(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        expected = _seed(capsule_dir, 7)
        ids, pages = _walk_pages(client, limit=3)
        assert ids == expected
        assert [len(p["items"]) for p in pages] == [3, 3, 1]
        # first page keeps total; v1-cursor pages omit it (spec)
        assert pages[0]["total"] == 7
        assert "total" not in pages[1]
        assert "total" not in pages[2]

    def test_no_skip_or_dupe_across_boundary_mutations(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        """Delete a returned row and insert a newer one mid-walk: the
        remainder is neither skipped nor duplicated (the offset failure)."""
        expected = _seed(capsule_dir, 6)
        resp = client.get("/v0/capsules?limit=2")
        page1 = resp.json()
        got = [i["run_id"] for i in page1["items"]]
        assert got == expected[:2]
        # Mutations across the boundary: remove one already-served capsule,
        # add a brand-new newest capsule.
        import shutil

        shutil.rmtree(capsule_dir / got[0])
        _write_capsule(capsule_dir, "01NEWER0000000000000000001",
                       created_at="2026-04-15T11:00:00+00:00")
        # Continue the walk from the stored cursor.
        rest: list[str] = []
        cursor = page1["next_cursor"]
        while cursor:
            body = client.get(f"/v0/capsules?limit=2&cursor={cursor}").json()
            rest.extend(i["run_id"] for i in body["items"])
            cursor = body["next_cursor"]
        assert rest == expected[2:]  # exactly the remainder, once each
        assert "01NEWER0000000000000000001" not in rest  # sorts before cursor

    def test_null_created_at_rows_sort_last_and_page_by_id(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        _write_capsule(capsule_dir, "01WITHTS0000000000000001",
                       created_at="2026-04-15T10:00:00+00:00")
        _write_capsule(capsule_dir, "01NOTS000000000000000002", created_at=None)
        _write_capsule(capsule_dir, "01NOTS000000000000000001", created_at=None)
        ids, _ = _walk_pages(client, limit=1)
        assert ids == [
            "01WITHTS0000000000000001",
            "01NOTS000000000000000002",
            "01NOTS000000000000000001",
        ]

    def test_invalid_cursor_is_400(self, client: TestClient, capsule_dir: Path) -> None:
        _seed(capsule_dir, 1)
        resp = client.get("/v0/capsules?cursor=@@@garbage@@@")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_cursor"

    def test_tampered_cursor_is_400(self, client: TestClient, capsule_dir: Path) -> None:
        _seed(capsule_dir, 3)
        good = client.get("/v0/capsules?limit=1").json()["next_cursor"]
        tampered = good[:-2] if len(good) > 2 else "xx"
        resp = client.get(f"/v0/capsules?cursor={tampered}")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_cursor"

    def test_legacy_offset_cursor_still_pages_with_deprecation_header(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        expected = _seed(capsule_dir, 3)
        resp = client.get(f"/v0/capsules?limit=1&cursor={encode_cursor(1)}")
        assert resp.status_code == 200
        assert resp.headers["Deprecation"] == "true"
        body = resp.json()
        assert body["total"] == 3  # legacy responses keep total
        assert body["items"][0]["run_id"] == expected[1]

    def test_keyset_responses_carry_no_deprecation_header(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        _seed(capsule_dir, 2)
        resp = client.get("/v0/capsules?limit=1")
        assert "Deprecation" not in resp.headers
        cursor = resp.json()["next_cursor"]
        resp2 = client.get(f"/v0/capsules?limit=1&cursor={cursor}")
        assert "Deprecation" not in resp2.headers

    def test_legacy_cursors_refused_after_sunset_flag(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        cfg = ServerConfig(db_path=str(db_path), insecure_no_auth=True)
        cfg.pagination.legacy_offset_cursors = False
        client = _make_client(cfg, capsule_dir)
        _seed(capsule_dir, 1)
        resp = client.get(f"/v0/capsules?cursor={encode_cursor(0)}")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_cursor"

    def test_steady_state_page_parses_zero_manifests(
        self, client: TestClient, capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The O(page) guard: once indexed, a list request re-reads no
        capsule.yaml (ADR-0206 acceptance: no per-page YAML re-parse)."""
        _seed(capsule_dir, 5)
        import novafabric.serve.capsule_loader as loader

        calls = {"n": 0}
        real = loader.load_capsule_manifest

        def counting(d: Path) -> dict[str, Any]:
            calls["n"] += 1
            return real(d)

        monkeypatch.setattr(loader, "load_capsule_manifest", counting)
        assert client.get("/v0/capsules").status_code == 200  # cold: backfills
        assert calls["n"] == 5
        calls["n"] = 0
        assert client.get("/v0/capsules").status_code == 200  # warm
        assert calls["n"] == 0


# --------------------------------------------------------------------------- #
# DELETE /v0/capsules/{run_id}
# --------------------------------------------------------------------------- #


def _hold_file(capsule_dir: Path, registry: str = "legal") -> Path:
    reg = capsule_dir.parent / "registries" / registry
    reg.mkdir(parents=True, exist_ok=True)
    return reg / "holds.jsonl"


def _add_hold(capsule_dir: Path, hold_id: str = "HOLD-1",
              released: bool = False) -> None:
    entry = {"hold_id": hold_id, "released_at": "2026-07-01T00:00:00+00:00" if released else None}
    with _hold_file(capsule_dir).open("a") as f:
        f.write(json.dumps(entry) + "\n")


class TestDeleteCapsule:
    def test_happy_path_removes_dir_indexes_and_audits(
        self, client: TestClient, capsule_dir: Path, db_path: Path
    ) -> None:
        rid = "01DEL0000000000000000001"
        _write_capsule(capsule_dir, rid)
        assert client.get("/v0/capsules").status_code == 200  # populate index
        conn = sqlite3.connect(db_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM runs_cache WHERE run_id=?", (rid,)
        ).fetchone()[0] == 1

        resp = client.delete(f"/v0/capsules/{rid}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"ok": True, "run_id": rid,
                        "audit": {"action": "capsule_delete"}}
        assert not (capsule_dir / rid).exists()
        assert conn.execute(
            "SELECT COUNT(*) FROM runs_cache WHERE run_id=?", (rid,)
        ).fetchone()[0] == 0
        # content-index rows gone too (table may exist from indexing)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "capsule_docs" in tables:
            assert conn.execute(
                "SELECT COUNT(*) FROM capsule_docs WHERE run_id=?", (rid,)
            ).fetchone()[0] == 0
        conn.close()
        entries = _audit_entries("capsule_delete")
        assert len(entries) == 1
        assert entries[0]["args"] == {"run_id": rid, "via": "api"}

    def test_unknown_id_404(self, client: TestClient) -> None:
        resp = client.delete("/v0/capsules/no-such-run")
        assert resp.status_code == 404

    def test_traversal_id_400(self, client: TestClient) -> None:
        resp = client.delete("/v0/capsules/evil..name")
        assert resp.status_code == 400

    def test_redelete_is_404_per_item(self, client: TestClient, capsule_dir: Path) -> None:
        rid = "01DEL0000000000000000002"
        _write_capsule(capsule_dir, rid)
        assert client.delete(f"/v0/capsules/{rid}").status_code == 200
        assert client.delete(f"/v0/capsules/{rid}").status_code == 404

    @pytest.mark.parametrize("roles", [["reader"], ["writer"], ["auditor"]])
    def test_non_admin_roles_forbidden(
        self, cfg: ServerConfig, capsule_dir: Path, roles: list[str]
    ) -> None:
        rid = "01DEL0000000000000000003"
        _write_capsule(capsule_dir, rid)
        c = _client_with_role(cfg, capsule_dir, roles)
        assert c.delete(f"/v0/capsules/{rid}").status_code == 403
        assert c.post(
            "/v0/capsules/bulk-delete", json={"run_ids": [rid]}
        ).status_code == 403
        assert (capsule_dir / rid).exists()  # nothing was deleted

    def test_admin_role_allowed(self, cfg: ServerConfig, capsule_dir: Path) -> None:
        rid = "01DEL0000000000000000004"
        _write_capsule(capsule_dir, rid)
        c = _client_with_role(cfg, capsule_dir, ["admin"])
        assert c.delete(f"/v0/capsules/{rid}").status_code == 200

    def test_active_hold_refuses_409_and_release_unblocks(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        rid = "01DEL0000000000000000005"
        _write_capsule(capsule_dir, rid)
        _add_hold(capsule_dir, "HOLD-A")
        resp = client.delete(f"/v0/capsules/{rid}")
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "legal_hold_active"
        assert err["details"]["hold_ids"] == ["HOLD-A"]
        assert (capsule_dir / rid).exists()
        refused = _audit_entries("capsule_delete_refused")
        assert len(refused) == 1
        assert refused[0]["args"]["run_id"] == rid
        # Releasing the hold unblocks (rewrite file with released hold).
        _hold_file(capsule_dir).write_text(
            json.dumps({"hold_id": "HOLD-A",
                        "released_at": "2026-07-02T00:00:00+00:00"}) + "\n"
        )
        assert client.delete(f"/v0/capsules/{rid}").status_code == 200

    def test_hold_ids_truncated_to_three(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        rid = "01DEL0000000000000000006"
        _write_capsule(capsule_dir, rid)
        for i in range(5):
            _add_hold(capsule_dir, f"HOLD-{i}")
        resp = client.delete(f"/v0/capsules/{rid}")
        assert resp.status_code == 409
        assert len(resp.json()["error"]["details"]["hold_ids"]) == 3

    def test_unexpired_worm_lock_refuses_409(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        from novafabric.storage._local_worm import LocalWormAdapter

        rid = "01DEL0000000000000000007"
        _write_capsule(capsule_dir, rid)
        reg = capsule_dir.parent / "registries" / "default"
        reg.mkdir(parents=True, exist_ok=True)
        LocalWormAdapter(reg / "worm.db").put(rid, b"payload", retention_days=30)
        resp = client.delete(f"/v0/capsules/{rid}")
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "worm_hold"
        assert "locked_until" in err["details"]
        assert (capsule_dir / rid).exists()


# --------------------------------------------------------------------------- #
# POST /v0/capsules/bulk-delete
# --------------------------------------------------------------------------- #


class TestBulkDelete:
    def test_mixed_batch_reports_per_item(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        a, b = "01BULK000000000000000001", "01BULK000000000000000002"
        _write_capsule(capsule_dir, a)
        _write_capsule(capsule_dir, b)
        resp = client.post("/v0/capsules/bulk-delete", json={
            "run_ids": [a, "missing-run", "bad..id", a, b],
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        outcomes = {(r["run_id"], r["outcome"]) for r in body["results"]}
        assert (a, "deleted") in outcomes
        assert (b, "deleted") in outcomes
        assert ("missing-run", "not_found") in outcomes
        assert ("bad..id", "invalid_id") in outcomes
        assert (a, "duplicate") in outcomes
        assert body["summary"] == {
            "requested": 5, "deleted": 2, "held": 0, "not_found": 1,
            "errors": 0, "invalid_id": 1, "duplicate": 1, "dry_run": False,
        }
        assert not (capsule_dir / a).exists()
        assert not (capsule_dir / b).exists()

    def test_batch_over_cap_is_422_before_any_work(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        cfg = ServerConfig(db_path=str(db_path), insecure_no_auth=True)
        cfg.bulk.max_items = 3
        client = _make_client(cfg, capsule_dir)
        rid = "01BULK000000000000000003"
        _write_capsule(capsule_dir, rid)
        resp = client.post("/v0/capsules/bulk-delete", json={
            "run_ids": [rid, "b", "c", "d"],
        })
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "bulk_batch_too_large"
        assert err["details"] == {"limit": 3, "received": 4}
        assert (capsule_dir / rid).exists()  # nothing deleted

    def test_empty_batch_is_400(self, client: TestClient) -> None:
        resp = client.post("/v0/capsules/bulk-delete", json={"run_ids": []})
        assert resp.status_code == 400

    def test_hold_marks_every_existing_item_held(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        a, b = "01BULK000000000000000004", "01BULK000000000000000005"
        _write_capsule(capsule_dir, a)
        _write_capsule(capsule_dir, b)
        _add_hold(capsule_dir, "HOLD-B")
        resp = client.post(
            "/v0/capsules/bulk-delete", json={"run_ids": [a, b, "gone"]}
        )
        assert resp.status_code == 200
        body = resp.json()
        by_id = {r["run_id"]: r for r in body["results"]}
        assert by_id[a] == {"run_id": a, "outcome": "held",
                            "code": "legal_hold_active"}
        assert by_id[b]["outcome"] == "held"
        assert by_id["gone"]["outcome"] == "not_found"
        assert body["summary"]["held"] == 2
        assert body["summary"]["deleted"] == 0
        assert (capsule_dir / a).exists()
        assert (capsule_dir / b).exists()

    def test_dry_run_reports_identically_and_deletes_nothing(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        a = "01BULK000000000000000006"
        _write_capsule(capsule_dir, a)
        resp = client.post("/v0/capsules/bulk-delete", json={
            "run_ids": [a, "missing"], "dry_run": True,
        })
        assert resp.status_code == 200
        body = resp.json()
        by_id = {r["run_id"]: r["outcome"] for r in body["results"]}
        assert by_id == {a: "deleted", "missing": "not_found"}
        assert body["summary"]["dry_run"] is True
        assert (capsule_dir / a).exists()  # nothing actually deleted
        # zero per-item audit entries; one summary entry with dry_run: true
        assert _audit_entries("capsule_delete") == []
        summaries = _audit_entries("capsule_bulk_delete")
        assert len(summaries) == 1
        assert summaries[0]["args"]["dry_run"] is True

    def test_real_bulk_writes_per_item_and_summary_audit(
        self, client: TestClient, capsule_dir: Path
    ) -> None:
        a, b = "01BULK000000000000000007", "01BULK000000000000000008"
        _write_capsule(capsule_dir, a)
        _write_capsule(capsule_dir, b)
        resp = client.post("/v0/capsules/bulk-delete", json={"run_ids": [a, b]})
        assert resp.status_code == 200
        per_item = _audit_entries("capsule_delete")
        assert {e["args"]["run_id"] for e in per_item} == {a, b}
        assert all(e["args"]["via"] == "bulk" for e in per_item)
        summaries = _audit_entries("capsule_bulk_delete")
        assert len(summaries) == 1
        assert summaries[0]["args"]["deleted"] == 2
        assert summaries[0]["args"]["bulk_id"] == per_item[0]["args"]["bulk_id"]


    def test_item_error_reported_without_rollback(
        self, client: TestClient, capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing removal yields outcome=error for that item only; the
        rest of the batch still proceeds (no transactional rollback)."""
        a, b = "01BULK000000000000000009", "01BULK000000000000000010"
        _write_capsule(capsule_dir, a)
        _write_capsule(capsule_dir, b)
        from novafabric.server import capsule_delete as cd

        real = cd.execute_delete

        def failing(cdir: Path, run_id: str, conn: Any) -> None:
            if run_id == a:
                raise OSError("disk says no")
            real(cdir, run_id, conn)

        monkeypatch.setattr(cd, "execute_delete", failing)
        resp = client.post("/v0/capsules/bulk-delete", json={"run_ids": [a, b]})
        assert resp.status_code == 200
        by_id = {r["run_id"]: r for r in resp.json()["results"]}
        assert by_id[a]["outcome"] == "error"
        assert by_id[a]["code"] == "delete_failed"
        assert "disk says no" in by_id[a]["message"]
        assert by_id[b]["outcome"] == "deleted"
        assert resp.json()["summary"]["errors"] == 1
        assert not (capsule_dir / b).exists()


# --------------------------------------------------------------------------- #
# Delete-core unit edges (holds/WORM tolerant parsing)
# --------------------------------------------------------------------------- #


class TestDeleteCoreEdges:
    def test_hold_scan_tolerates_blank_and_malformed_lines(
        self, capsule_dir: Path
    ) -> None:
        from novafabric.server.capsule_delete import active_hold_ids

        f = _hold_file(capsule_dir)
        f.write_text(
            "\n"  # blank
            "not json at all\n"
            + json.dumps({"hold_id": "H-GOOD", "released_at": None}) + "\n"
            + json.dumps({"hold_id": "H-DONE", "released_at": "2026-01-01"}) + "\n"
        )
        assert active_hold_ids(capsule_dir) == ["H-GOOD"]

    def test_worm_lock_ignores_other_capsules(self, capsule_dir: Path) -> None:
        from novafabric.server.capsule_delete import worm_locked_until
        from novafabric.storage._local_worm import LocalWormAdapter

        reg = capsule_dir.parent / "registries" / "default"
        reg.mkdir(parents=True, exist_ok=True)
        adapter = LocalWormAdapter(reg / "worm.db")
        adapter.put("other-run", b"x", retention_days=30)
        assert worm_locked_until(capsule_dir, "my-run") is None
        adapter.put("my-run", b"y", retention_days=30)
        locked = worm_locked_until(capsule_dir, "my-run")
        assert locked is not None and locked.tzinfo is not None

    def test_unreadable_worm_db_is_skipped(self, capsule_dir: Path) -> None:
        from novafabric.server.capsule_delete import worm_locked_until

        reg = capsule_dir.parent / "registries" / "broken"
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "worm.db").write_bytes(b"this is not sqlite")
        assert worm_locked_until(capsule_dir, "any-run") is None

    def test_upload_index_failure_never_blocks_upload(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io
        import zipfile

        from novafabric.server import capsule_index as ci

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("index down")

        monkeypatch.setattr(ci, "upsert_capsule", boom)
        rid = "01UP00000000000000000001"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("capsule.yaml", yaml.safe_dump({
                "run_id": rid, "status": "success",
                "created_at": "2026-04-15T10:00:00+00:00",
            }))
        buf.seek(0)
        resp = client.post(
            "/v0/capsules",
            files={"capsule": ("c.zip", buf, "application/zip")},
        )
        assert resp.status_code == 201  # fail-open: upload unaffected


# --------------------------------------------------------------------------- #
# Config (spec §Config keys)
# --------------------------------------------------------------------------- #


class TestBulkOpsConfig:
    def test_defaults(self) -> None:
        cfg = ServerConfig()
        assert cfg.bulk.max_items == 100
        assert cfg.pagination.legacy_offset_cursors is True

    def test_max_items_ceiling_is_1000(self) -> None:
        with pytest.raises(Exception):
            BulkConfig(max_items=1001)
        with pytest.raises(Exception):
            BulkConfig(max_items=0)
        assert BulkConfig(max_items=1000).max_items == 1000

    def test_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_BULK_MAX_ITEMS", "250")
        monkeypatch.setenv(
            "NOVAFABRIC_SERVER_PAGINATION_LEGACY_OFFSET_CURSORS", "false"
        )
        cfg = ServerConfig()
        assert cfg.bulk.max_items == 250
        assert cfg.pagination.legacy_offset_cursors is False

    def test_env_override_respects_ceiling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_SERVER_BULK_MAX_ITEMS", "5000")
        with pytest.raises(Exception):
            ServerConfig()
