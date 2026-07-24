"""Tests for ADR-0208 usage metering (P1): ledger, counters, rollups, /v0/usage.

Contract: design/spec/usage-metering-v0.md
  - ledger + counter in one transaction; replayed (metric, ref) inserts are
    no-ops (idempotent counting by construction, not by hope)
  - upload metering: capsules_created amount 1 + bytes_stored = unpacked
    bytes, ref = run_id, AFTER the atomic publish; duplicate upload (409)
    never double counts; metering failure never fails the upload
  - deletes append negative rows (ref "<run_id>:delete"); pre-metering
    capsules record nothing (attribution refused, drift covers them)
  - lazy rollup finalization at the first write of a new period; write-once;
    retention-bounded pruning of rollups/ledger/counters
  - GET /v0/usage: admin/auditor see all + global/drift; members see only
    membership workspaces (filtering, not 403); empty period is [], never 500
  - api_requests: bounded LRU accumulator, flushed at most once per interval
  - feature off (master switch) => zero accounting, no tables touched
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server import usage  # noqa: E402
from novafabric.server.api_keys import create_key  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import (  # noqa: E402
    RateLimitsConfig,
    ServerConfig,
    UsageConfig,
)
from novafabric.server.usage import (  # noqa: E402
    METRIC_API_REQUESTS,
    METRIC_BYTES,
    METRIC_CAPSULES,
    ApiRequestAccumulator,
    Attribution,
    LedgerEntry,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_ATT = Attribution(workspace="default", org="default", source="default")


def _client(cfg: ServerConfig) -> TestClient:
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def _config(db_path: Path, *, metering: bool = True, **usage_kwargs: object) -> ServerConfig:
    return ServerConfig(
        db_path=str(db_path),
        insecure_no_auth=True,  # ADR-0184 opt-out: anonymous admin for tests
        rate_limits=RateLimitsConfig(enabled=metering),
        usage=UsageConfig(**usage_kwargs),  # type: ignore[arg-type]
    )


def _capsule_zip(run_id: str, payload_bytes: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("capsule.yaml", f"run_id: {run_id}\nstatus: completed\n")
        if payload_bytes:
            zf.writestr("blob.bin", "x" * payload_bytes)
    return buf.getvalue()


def _upload(
    client: TestClient, run_id: str, payload_bytes: int = 0, token: str | None = None
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(
        "/v0/capsules",
        files={
            "capsule": (
                f"{run_id}.zip",
                _capsule_zip(run_id, payload_bytes),
                "application/zip",
            )
        },
        headers=headers,
    )


def _counter(db: Path, workspace: str, metric: str, period: str | None = None) -> int:
    period = period or usage.period_for()
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT total FROM usage_counters WHERE workspace=? AND period=?"
            " AND metric=?",
            (workspace, period, metric),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def _ledger_rows(db: Path, **where: object) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        clauses = " AND ".join(f"{k}=?" for k in where)
        sql = "SELECT * FROM usage_ledger"
        if clauses:
            sql += f" WHERE {clauses}"
        return [dict(r) for r in conn.execute(sql, tuple(where.values()))]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage-test.db"
    # Align the default registry path (api-key auth resolves it env-side).
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    return db


@pytest.fixture
def capsule_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cdir = tmp_path / "capsules"
    cdir.mkdir()
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(cdir))
    return cdir


@pytest.fixture
def audit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(path))
    return path


def _dt(year: int, month: int, day: int = 15) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _entry(metric: str, amount: int, ref: str | None, workspace: str = "default") -> LedgerEntry:
    return LedgerEntry(
        metric=metric,
        amount=amount,
        ref=ref,
        workspace=workspace,
        org="default",
        attribution="default",
        actor="test",
    )


# --------------------------------------------------------------------------- #
# Ledger + counter store semantics
# --------------------------------------------------------------------------- #


class TestLedgerStore:
    def test_record_writes_ledger_and_counter_atomically(self, db_path: Path) -> None:
        n = usage.record_entries(
            [_entry(METRIC_CAPSULES, 1, "r1"), _entry(METRIC_BYTES, 42, "r1")],
            db_path=db_path,
        )
        assert n == 2
        assert _counter(db_path, "default", METRIC_CAPSULES) == 1
        assert _counter(db_path, "default", METRIC_BYTES) == 42
        rows = _ledger_rows(db_path, ref="r1")
        assert {r["metric"] for r in rows} == {METRIC_CAPSULES, METRIC_BYTES}

    def test_replayed_insert_is_a_noop(self, db_path: Path) -> None:
        # Second guard of the spec's idempotency: same (metric, ref) replay
        # inserts zero rows and skips the counter update.
        first = usage.record_capsule_upload(
            run_id="r-dup", size_bytes=100, attribution=_ATT, actor="t", db_path=db_path
        )
        replay = usage.record_capsule_upload(
            run_id="r-dup", size_bytes=100, attribution=_ATT, actor="t", db_path=db_path
        )
        assert (first, replay) == (2, 0)
        assert _counter(db_path, "default", METRIC_CAPSULES) == 1
        assert _counter(db_path, "default", METRIC_BYTES) == 100
        assert len(_ledger_rows(db_path, ref="r-dup")) == 2

    def test_null_ref_rows_are_never_deduplicated(self, db_path: Path) -> None:
        usage.record_entries([_entry(METRIC_API_REQUESTS, 5, None)], db_path=db_path)
        usage.record_entries([_entry(METRIC_API_REQUESTS, 3, None)], db_path=db_path)
        assert _counter(db_path, "default", METRIC_API_REQUESTS) == 8

    def test_delete_appends_negative_mirror_rows(self, db_path: Path) -> None:
        att = Attribution(workspace="team-a", org="acme", source="key")
        usage.record_capsule_upload(
            run_id="r-del", size_bytes=250, attribution=att, actor="t", db_path=db_path
        )
        ws = usage.record_capsule_delete(run_id="r-del", actor="t", db_path=db_path)
        assert ws == "team-a"
        assert _counter(db_path, "team-a", METRIC_CAPSULES) == 0
        assert _counter(db_path, "team-a", METRIC_BYTES) == 0
        neg = _ledger_rows(db_path, ref="r-del:delete")
        assert sorted(r["amount"] for r in neg) == [-250, -1]
        # Ledger stays append-only: the original positive rows are untouched.
        assert sorted(r["amount"] for r in _ledger_rows(db_path, ref="r-del")) == [1, 250]
        # Replayed delete adjustment is a no-op too ((metric, ref) unique).
        usage.record_capsule_delete(run_id="r-del", actor="t", db_path=db_path)
        assert _counter(db_path, "team-a", METRIC_CAPSULES) == 0
        assert len(_ledger_rows(db_path, ref="r-del:delete")) == 2

    def test_delete_of_unmetered_capsule_records_nothing(self, db_path: Path) -> None:
        # Pre-metering capsule: attribution would be guesswork — refused.
        assert (
            usage.record_capsule_delete(run_id="ghost", actor="t", db_path=db_path)
            is None
        )
        assert _ledger_rows(db_path) == []


# --------------------------------------------------------------------------- #
# Lazy rollups + retention
# --------------------------------------------------------------------------- #


class TestRollups:
    def _rollups(self, db: Path) -> list[dict]:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM usage_rollups")]
        finally:
            conn.close()

    def test_first_write_of_new_period_finalizes_previous(self, db_path: Path) -> None:
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 3, None)], db_path=db_path, now=_dt(2026, 6)
        )
        assert self._rollups(db_path) == []  # nothing to finalize yet
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 1, None)], db_path=db_path, now=_dt(2026, 7)
        )
        rollups = self._rollups(db_path)
        assert len(rollups) == 1
        assert rollups[0]["period"] == "2026-06"
        assert rollups[0]["total"] == 3
        assert rollups[0]["workspace"] == "default"
        assert rollups[0]["finalized_at"]

    def test_refinalization_is_a_noop(self, db_path: Path) -> None:
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 3, None)], db_path=db_path, now=_dt(2026, 6)
        )
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 1, None)], db_path=db_path, now=_dt(2026, 7)
        )
        # Tamper with the (still retained) counter row, then write again:
        # the finalized rollup total must NOT change (write-once).
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE usage_counters SET total = 999 WHERE period = '2026-06'"
        )
        conn.commit()
        conn.close()
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 1, None)], db_path=db_path, now=_dt(2026, 7, 20)
        )
        rollups = [r for r in self._rollups(db_path) if r["period"] == "2026-06"]
        assert len(rollups) == 1
        assert rollups[0]["total"] == 3

    def test_retention_prunes_rollups_ledger_and_counters(self, db_path: Path) -> None:
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 2, "old-run")], db_path=db_path, now=_dt(2026, 6)
        )
        # 26 months later: 2026-06 is past both bounds (rollup 24, ledger 3).
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 1, None)], db_path=db_path, now=_dt(2028, 8)
        )
        assert all(r["period"] != "2026-06" for r in self._rollups(db_path))
        assert _ledger_rows(db_path, period="2026-06") == []
        assert _counter(db_path, "default", METRIC_CAPSULES, period="2026-06") == 0

    def test_ledger_retention_keeps_recent_finalized_periods(self, db_path: Path) -> None:
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 2, "kept-run")], db_path=db_path, now=_dt(2026, 6)
        )
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 1, None)], db_path=db_path, now=_dt(2026, 8)
        )
        # 2026-06 is finalized but within the 3-month ledger retention.
        assert len(_ledger_rows(db_path, period="2026-06")) == 1
        assert any(r["period"] == "2026-06" for r in self._rollups(db_path))

    def test_all_time_totals_never_double_count_finalized_periods(
        self, db_path: Path
    ) -> None:
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 3, None)], db_path=db_path, now=_dt(2026, 6)
        )
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 2, None)], db_path=db_path, now=_dt(2026, 7)
        )
        # 2026-06: rollup AND retained counter both exist — count once.
        totals = usage.all_time_totals(db_path=db_path)
        assert totals["default"][METRIC_CAPSULES] == 5

    def test_usage_for_period_serves_past_from_rollups(self, db_path: Path) -> None:
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 3, None)], db_path=db_path, now=_dt(2026, 6)
        )
        usage.record_entries(
            [_entry(METRIC_CAPSULES, 2, None)], db_path=db_path, now=_dt(2026, 7)
        )
        past = usage.usage_for_period("2026-06", db_path=db_path)
        assert len(past) == 1
        assert past[0]["metrics"][METRIC_CAPSULES] == 3
        assert usage.usage_for_period("1999-01", db_path=db_path) == []

    def test_months_before(self) -> None:
        assert usage._months_before("2026-07", 3) == "2026-04"
        assert usage._months_before("2026-02", 3) == "2025-11"
        assert usage._months_before("2026-01", 24) == "2024-01"


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #


class TestAttribution:
    def _seed_workspaces(self, db: Path) -> tuple[str, str]:
        from novafabric.server import workspace_store

        org_id, _ = workspace_store.ensure_default(db_path=db)
        acme = workspace_store.create_org("acme", "Acme", "test", db_path=db)
        ws = workspace_store.create_workspace(
            acme["id"], "team-a", "Team A", "test", db_path=db
        )
        return acme["id"], ws["id"]

    def test_key_binding_wins(self, db_path: Path) -> None:
        from novafabric.server.auth import AuthContext

        self._seed_workspaces(db_path)
        att = usage.resolve_attribution(
            AuthContext(subject="a@x", roles=["writer"], workspace="team-a"), db_path
        )
        assert att == Attribution(workspace="team-a", org="acme", source="key")

    def test_single_membership_used_when_no_binding(self, db_path: Path) -> None:
        from novafabric.server import workspace_store
        from novafabric.server.auth import AuthContext

        _, ws_id = self._seed_workspaces(db_path)
        workspace_store.add_membership(
            "bob@x", "workspace", ws_id, "reader", "test", db_path=db_path
        )
        att = usage.resolve_attribution(
            AuthContext(subject="bob@x", roles=["reader"]), db_path
        )
        assert att == Attribution(
            workspace="team-a", org="acme", source="membership"
        )

    def test_ambiguous_membership_resolves_to_default(self, db_path: Path) -> None:
        from novafabric.server import workspace_store
        from novafabric.server.auth import AuthContext

        acme_id, ws_id = self._seed_workspaces(db_path)
        ws2 = workspace_store.create_workspace(
            acme_id, "team-b", "Team B", "test", db_path=db_path
        )
        for wid in (ws_id, ws2["id"]):
            workspace_store.add_membership(
                "eve@x", "workspace", wid, "reader", "test", db_path=db_path
            )
        att = usage.resolve_attribution(
            AuthContext(subject="eve@x", roles=["reader"]), db_path
        )
        assert att.source == "default"
        assert att.workspace == "default"

    def test_no_auth_and_missing_tables_resolve_to_default(
        self, db_path: Path
    ) -> None:
        assert usage.resolve_attribution(None, db_path).source == "default"


# --------------------------------------------------------------------------- #
# Upload accounting (route level)
# --------------------------------------------------------------------------- #


class TestUploadAccounting:
    def test_upload_meters_count_and_unpacked_bytes(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path))
        assert _upload(client, "r-acct", payload_bytes=300).status_code == 201
        # Bytes accounting matches the published (unpacked) on-disk size.
        expected = sum(
            f.stat().st_size
            for f in (capsule_dir / "r-acct").rglob("*")
            if f.is_file()
        )
        assert expected >= 300
        assert _counter(db_path, "default", METRIC_CAPSULES) == 1
        assert _counter(db_path, "default", METRIC_BYTES) == expected
        rows = _ledger_rows(db_path, ref="r-acct")
        assert {r["metric"]: r["amount"] for r in rows} == {
            METRIC_CAPSULES: 1,
            METRIC_BYTES: expected,
        }
        assert all(r["attribution"] == "default" for r in rows)

    def test_duplicate_409_retry_never_double_counts(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path))
        assert _upload(client, "r-409").status_code == 201
        assert _upload(client, "r-409").status_code == 409
        assert _counter(db_path, "default", METRIC_CAPSULES) == 1
        assert len(_ledger_rows(db_path, ref="r-409")) == 2

    def test_api_key_workspace_binding_attributes_the_upload(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        from novafabric.server import workspace_store

        org_id, _ = workspace_store.ensure_default(db_path=db_path)
        acme = workspace_store.create_org("acme", "Acme", "t", db_path=db_path)
        workspace_store.create_workspace(
            acme["id"], "team-a", "Team A", "t", db_path=db_path
        )
        key, _rec = create_key(
            "svc@x", ["writer"], actor="t", workspace="team-a", db_path=db_path
        )
        client = _client(_config(db_path))
        assert _upload(client, "r-key", token=key).status_code == 201
        rows = _ledger_rows(db_path, ref="r-key")
        assert all(r["workspace"] == "team-a" for r in rows)
        assert all(r["org"] == "acme" for r in rows)
        assert all(r["attribution"] == "key" for r in rows)
        assert all(r["actor"] == "svc@x" for r in rows)

    def test_metering_failure_never_fails_the_upload(
        self,
        db_path: Path,
        capsule_dir: Path,
        audit_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _boom(**_kwargs: object) -> int:
            raise RuntimeError("metering store exploded")

        monkeypatch.setattr("novafabric.server.usage.record_capsule_upload", _boom)
        client = _client(_config(db_path))
        resp = _upload(client, "r-faulty")
        assert resp.status_code == 201  # fault injection: upload unharmed
        assert (capsule_dir / "r-faulty" / "capsule.yaml").exists()
        # Spec: metering failures are logged + audited.
        assert audit_file.exists()
        assert "capsule_upload_metering_failed" in audit_file.read_text()

    def test_master_switch_off_means_zero_accounting(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        # Regression pin: flag off => no usage tables, no accumulator,
        # no workspace checker, upload behavior unchanged.
        client = _client(_config(db_path, metering=False))
        assert _upload(client, "r-off").status_code == 201
        assert getattr(client.app.state, "usage_accumulator", None) is None
        assert getattr(client.app.state, "workspace_quota_checker", None) is None
        conn = sqlite3.connect(db_path)
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        assert "usage_ledger" not in tables
        assert "usage_counters" not in tables

    def test_metering_kill_switch_off_keeps_rate_limits_on(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path, metering=True, metering_enabled=False))
        assert _upload(client, "r-kill").status_code == 201
        assert _ledger_rows(db_path) == []
        assert getattr(client.app.state, "usage_accumulator", None) is None


# --------------------------------------------------------------------------- #
# Delete accounting (route level)
# --------------------------------------------------------------------------- #


class TestDeleteAccounting:
    def test_delete_appends_negative_rows(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        client = _client(_config(db_path))
        assert _upload(client, "r-d1", payload_bytes=100).status_code == 201
        bytes_metered = _counter(db_path, "default", METRIC_BYTES)
        assert bytes_metered > 0
        assert client.delete("/v0/capsules/r-d1").status_code == 200
        assert _counter(db_path, "default", METRIC_CAPSULES) == 0
        assert _counter(db_path, "default", METRIC_BYTES) == 0
        neg = _ledger_rows(db_path, ref="r-d1:delete")
        assert sorted(r["amount"] for r in neg) == [-bytes_metered, -1]

    def test_bulk_delete_appends_negative_rows_per_item(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        client = _client(_config(db_path))
        for rid in ("r-b1", "r-b2"):
            assert _upload(client, rid).status_code == 201
        assert _counter(db_path, "default", METRIC_CAPSULES) == 2
        resp = client.post(
            "/v0/capsules/bulk-delete", json={"run_ids": ["r-b1", "r-b2"]}
        )
        assert resp.status_code == 200
        assert resp.json()["summary"]["deleted"] == 2
        assert _counter(db_path, "default", METRIC_CAPSULES) == 0
        assert len(_ledger_rows(db_path, metric=METRIC_CAPSULES)) == 4  # 2 pos + 2 neg

    def test_bulk_delete_dry_run_records_nothing(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        client = _client(_config(db_path))
        assert _upload(client, "r-dry").status_code == 201
        resp = client.post(
            "/v0/capsules/bulk-delete",
            json={"run_ids": ["r-dry"], "dry_run": True},
        )
        assert resp.status_code == 200
        assert _counter(db_path, "default", METRIC_CAPSULES) == 1
        assert _ledger_rows(db_path, ref="r-dry:delete") == []

    def test_delete_of_pre_metering_capsule_is_clean(
        self, db_path: Path, capsule_dir: Path, audit_file: Path
    ) -> None:
        # Seeded before metering ever ran: delete succeeds, no negative rows.
        dest = capsule_dir / "r-old"
        dest.mkdir()
        (dest / "capsule.yaml").write_text("run_id: r-old\n")
        client = _client(_config(db_path))
        assert client.delete("/v0/capsules/r-old").status_code == 200
        assert _ledger_rows(db_path) == []


# --------------------------------------------------------------------------- #
# GET /v0/usage
# --------------------------------------------------------------------------- #


class TestUsageEndpoint:
    def test_admin_sees_all_workspaces_global_and_drift(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path))
        assert _upload(client, "r-u1", payload_bytes=50).status_code == 201
        # One pre-metering capsule: appears only in global/drift (spec).
        dest = capsule_dir / "r-pre"
        dest.mkdir()
        (dest / "capsule.yaml").write_text("run_id: r-pre\n")

        resp = client.get("/v0/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["period"] == usage.period_for()
        assert len(body["workspaces"]) == 1
        ws = body["workspaces"][0]
        assert ws["workspace"] == "default"
        assert ws["org"] == "default"
        assert ws["metrics"][METRIC_CAPSULES] == 1
        assert ws["metrics"][METRIC_BYTES] > 0
        assert body["orgs"] == [
            {"org": "default", "metrics": ws["metrics"]}
        ]
        assert body["global"]["capsules"] == 2
        assert body["global"]["source"] == "measure_capsule_store"
        # Drift = derived minus metered: exactly the pre-metering capsule.
        assert body["drift"]["capsules"] == 1
        expected_pre_bytes = (dest / "capsule.yaml").stat().st_size
        assert body["drift"]["bytes"] == expected_pre_bytes
        assert body["next_cursor"] is None

    def test_member_sees_only_membership_workspaces_no_global(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        from novafabric.server import workspace_store

        workspace_store.ensure_default(db_path=db_path)
        acme = workspace_store.create_org("acme", "Acme", "t", db_path=db_path)
        ws = workspace_store.create_workspace(
            acme["id"], "team-a", "Team A", "t", db_path=db_path
        )
        writer_key, _ = create_key(
            "svc@x", ["writer"], actor="t", workspace="team-a", db_path=db_path
        )
        reader_key, _ = create_key("bob@x", ["reader"], actor="t", db_path=db_path)
        workspace_store.add_membership(
            "bob@x", "workspace", ws["id"], "reader", "t", db_path=db_path
        )
        client = _client(_config(db_path))
        # team-a gets one capsule (via key binding), default gets one.
        assert _upload(client, "r-team", token=writer_key).status_code == 201
        assert _upload(client, "r-default").status_code == 201

        resp = client.get(
            "/v0/usage", headers={"Authorization": f"Bearer {reader_key}"}
        )
        assert resp.status_code == 200  # filtering, not 403 (spec RBAC)
        body = resp.json()
        assert [w["workspace"] for w in body["workspaces"]] == ["team-a"]
        assert "global" not in body
        assert "drift" not in body
        # Org rollup covers only the visible workspaces (no leak).
        assert [o["org"] for o in body["orgs"]] == ["acme"]

    def test_org_scoped_membership_expands_to_org_workspaces(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        from novafabric.server import workspace_store

        workspace_store.ensure_default(db_path=db_path)
        acme = workspace_store.create_org("acme", "Acme", "t", db_path=db_path)
        workspace_store.create_workspace(
            acme["id"], "team-a", "Team A", "t", db_path=db_path
        )
        writer_key, _ = create_key(
            "svc@x", ["writer"], actor="t", workspace="team-a", db_path=db_path
        )
        org_key, _ = create_key("carol@x", ["reader"], actor="t", db_path=db_path)
        workspace_store.add_membership(
            "carol@x", "org", acme["id"], "reader", "t", db_path=db_path
        )
        client = _client(_config(db_path))
        assert _upload(client, "r-team2", token=writer_key).status_code == 201
        body = client.get(
            "/v0/usage", headers={"Authorization": f"Bearer {org_key}"}
        ).json()
        assert [w["workspace"] for w in body["workspaces"]] == ["team-a"]

    def test_visibility_filter_fails_closed_on_store_error(
        self, db_path: Path, capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader_key, _ = create_key("dan@x", ["reader"], actor="t", db_path=db_path)
        client = _client(_config(db_path))
        assert _upload(client, "r-any").status_code == 201

        def _boom(**_kwargs: object) -> list:
            raise RuntimeError("store down")

        monkeypatch.setattr(
            "novafabric.server.workspace_store.list_workspaces", _boom
        )
        resp = client.get(
            "/v0/usage", headers={"Authorization": f"Bearer {reader_key}"}
        )
        assert resp.status_code == 200
        assert resp.json()["workspaces"] == []  # fail closed: nothing leaks

    def test_quota_block_shown_only_for_budgeted_workspaces(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        from novafabric.server.config import QuotaConfig, WorkspaceQuotaConfig

        cfg = ServerConfig(
            db_path=str(db_path),
            insecure_no_auth=True,
            rate_limits=RateLimitsConfig(
                enabled=True,
                quota=QuotaConfig(
                    workspaces={
                        "default": WorkspaceQuotaConfig(
                            max_capsules_soft=10, max_capsules_hard=20
                        )
                    }
                ),
            ),
        )
        client = _client(cfg)
        assert _upload(client, "r-q1", payload_bytes=30).status_code == 201
        usage.record_entries(  # a second, unbudgeted workspace
            [_entry(METRIC_CAPSULES, 2, None, workspace="team-x")],
            db_path=db_path,
        )
        body = client.get("/v0/usage").json()
        by_ws = {w["workspace"]: w for w in body["workspaces"]}
        quota = by_ws["default"]["quota"]
        assert quota["capsules"] == {"usage": 1, "soft": 10, "hard": 20}
        assert quota["bytes"]["usage"] > 0
        assert quota["bytes"]["soft"] == 0  # 0 = unlimited, reported as-is
        assert "quota" not in by_ws["team-x"]

    def test_empty_state_is_empty_list_never_500(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path))
        resp = client.get("/v0/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["workspaces"] == []
        assert body["orgs"] == []
        assert body["global"]["capsules"] == 0
        resp = client.get("/v0/usage?period=2020-01")
        assert resp.status_code == 200
        assert resp.json()["workspaces"] == []

    def test_invalid_period_is_400(self, db_path: Path, capsule_dir: Path) -> None:
        client = _client(_config(db_path))
        for bad in ("2026", "2026-13", "junk", "2026-7"):
            resp = client.get(f"/v0/usage?period={bad}")
            assert resp.status_code == 400
            assert resp.json()["error"]["code"] == "invalid_period"

    def test_workspace_filter_and_pagination(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        for i, ws in enumerate(("alpha", "beta", "gamma")):
            usage.record_entries(
                [_entry(METRIC_CAPSULES, i + 1, None, workspace=ws)],
                db_path=db_path,
            )
        client = _client(_config(db_path))
        resp = client.get("/v0/usage?workspace=beta")
        assert [w["workspace"] for w in resp.json()["workspaces"]] == ["beta"]
        page1 = client.get("/v0/usage?limit=2").json()
        assert len(page1["workspaces"]) == 2
        assert page1["next_cursor"]
        page2 = client.get(f"/v0/usage?limit=2&cursor={page1['next_cursor']}").json()
        assert [w["workspace"] for w in page2["workspaces"]] == ["gamma"]
        assert page2["next_cursor"] is None

    def test_requires_authentication_when_secured(
        self, db_path: Path, capsule_dir: Path, tmp_path: Path
    ) -> None:
        cfg = ServerConfig(db_path=str(db_path))  # local-token auth (ADR-0184)
        client = _client(cfg)
        assert client.get("/v0/usage").status_code == 401


# --------------------------------------------------------------------------- #
# api_requests accumulator
# --------------------------------------------------------------------------- #


class TestApiRequestAccumulator:
    def test_flush_writes_one_row_per_workspace(self, db_path: Path) -> None:
        acc = ApiRequestAccumulator(flush_interval_s=60.0)
        acc.add("default")
        acc.add("default")
        acc.add("team-a", 3)
        assert acc.flush(db_path) == 0  # not due, not forced — no write
        assert acc.flush(db_path, force=True) == 2
        assert _counter(db_path, "default", METRIC_API_REQUESTS) == 2
        assert _counter(db_path, "team-a", METRIC_API_REQUESTS) == 3
        assert acc.flush(db_path, force=True) == 0  # drained

    def test_interval_gates_the_flush(self, db_path: Path) -> None:
        now = [0.0]
        acc = ApiRequestAccumulator(flush_interval_s=10.0, clock=lambda: now[0])
        acc.add("default")
        assert not acc.due()
        now[0] += 10.1
        assert acc.due()
        assert acc.flush(db_path) == 1

    def test_lru_bound_holds(self, db_path: Path) -> None:
        acc = ApiRequestAccumulator(max_entries=2, flush_interval_s=60.0)
        for ws in ("a", "b", "c"):
            acc.add(ws)
        assert acc.flush(db_path, force=True) == 2  # oldest ('a') was evicted
        assert _counter(db_path, "a", METRIC_API_REQUESTS) == 0

    def test_middleware_feeds_and_flushes(
        self, db_path: Path, capsule_dir: Path
    ) -> None:
        client = _client(_config(db_path, flush_interval_s=0.001))
        assert getattr(client.app.state, "usage_accumulator", None) is not None
        client.get("/v0/capsules")  # authenticated route — auth state exists
        client.get("/v0/capsules")  # second request: interval elapsed => flush
        assert _counter(db_path, "default", METRIC_API_REQUESTS) >= 1
        rows = _ledger_rows(db_path, metric=METRIC_API_REQUESTS)
        assert rows
        assert all(r["ref"] is None for r in rows)
        assert all(r["actor"] == "system" for r in rows)
