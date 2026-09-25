"""Tests for ADR-0208 P2: `nova server usage reconcile` + chargeback export.

Acceptance criteria (ADR-0208 "Reconciliation"; spec usage-metering-v0.md):

Reconcile
  - report-only by default: drift = derived (measure_capsule_store) minus
    metered (ledger all-time sums); no ledger row is written without --apply
  - --apply appends one signed (+/-) row per drifting metric to the DEFAULT
    workspace only, attribution='reconciliation', ref='recon:<timestamp>';
    existing ledger rows are never rewritten; counters move only through
    the appended row; a second apply is a no-op (drift is zero)
  - a negative adjustment that would drive the default workspace below zero
    is refused (the excess belongs to another workspace — guessing refused)
  - every run appends a `usage.reconcile` entry to the hash-chained audit
    log; audit failure: report-only degrades (audited=False), apply raises
Export
  - one row per (period, org, workspace, metric), deterministic ordering;
    finalized periods from rollups (final), others from counters (provisional)
  - RFC 4180 CSV (CRLF, header, quoting) with formula-injection-safe text
    cells; integer cells (incl. negative totals) never rewritten
  - NDJSON: one sorted-key object per line; byte-identical on re-export
  - malformed / inverted / over-wide period ranges are refused
CLI
  - `nova server usage reconcile|export` exit codes 0 / 1 / 2 and help
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from novafabric.audit import AuditLog
from novafabric.audit import _paths as audit_paths
from novafabric.cli.main import app
from novafabric.server import usage, usage_export
from novafabric.server.usage import METRIC_BYTES, METRIC_CAPSULES, Attribution
from novafabric.server.usage_export import (
    ChargebackRow,
    InvalidPeriodRangeError,
    chargeback_rows,
    periods_between,
    render,
    safe_cell,
    to_csv,
    to_ndjson,
)
from novafabric.server.usage_reconcile import (
    DriftReport,
    ReconciliationAuditError,
    ReconciliationRefusedError,
    adjustment_entries,
    measure_drift,
    reconcile,
)

NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
_DEFAULT = Attribution(workspace="default", org="default", source="default")
_TEAM_A = Attribution(workspace="team-a", org="acme", source="key")

runner = CliRunner()


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _close_anchors() -> Iterator[None]:
    yield
    usage.close_anchors()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "usage-p2.db"
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(db))
    return db


@pytest.fixture
def capsule_dir(tmp_path: Path) -> Path:
    cdir = tmp_path / "capsules"
    cdir.mkdir()
    return cdir


@pytest.fixture
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit_paths, "AUDIT_LOG_PATH", path)
    return path


def _capsule(store: Path, run_id: str, payload: int = 0) -> int:
    """Create one capsule dir in *store*; return its unpacked size."""
    cdir = store / run_id
    cdir.mkdir()
    (cdir / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    if payload:
        (cdir / "blob.bin").write_bytes(b"x" * payload)
    return usage.dir_size_bytes(cdir)


def _meter(db: Path, run_id: str, size: int, att: Attribution = _DEFAULT) -> None:
    usage.record_capsule_upload(
        run_id=run_id, size_bytes=size, attribution=att, actor="t", db_path=db, now=NOW
    )


def _ledger(db: Path) -> list[dict[str, Any]]:
    conn = usage.open_usage_db(db)  # ensures the tables exist
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM usage_ledger ORDER BY ledger_id")]
    finally:
        conn.close()


def _audit_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _report(**kw: int) -> DriftReport:
    base = {
        "derived_capsules": 0,
        "derived_bytes": 0,
        "metered_capsules": 0,
        "metered_bytes": 0,
        "drift_capsules": 0,
        "drift_bytes": 0,
    }
    base.update(kw)
    return DriftReport(checked_at=NOW.isoformat(), capsule_dir="/x", **base)


# --------------------------------------------------------------------------- #
# Reconcile — measurement
# --------------------------------------------------------------------------- #


class TestMeasureDrift:
    def test_in_sync_when_every_capsule_is_metered(self, db_path: Path, capsule_dir: Path) -> None:
        _meter(db_path, "r1", _capsule(capsule_dir, "r1", 100))
        rep = measure_drift(capsule_dir, db_path=db_path, now=NOW)
        assert rep.in_sync
        assert rep.derived_capsules == rep.metered_capsules == 1
        assert rep.checked_at == NOW.isoformat()

    def test_pre_metering_capsule_is_positive_drift(self, db_path: Path, capsule_dir: Path) -> None:
        _meter(db_path, "r1", _capsule(capsule_dir, "r1"))
        size = _capsule(capsule_dir, "legacy", 50)
        rep = measure_drift(capsule_dir, db_path=db_path, now=NOW)
        assert (rep.drift_capsules, rep.drift_bytes) == (1, size)
        assert not rep.in_sync

    def test_missing_store_measures_zero(self, db_path: Path, tmp_path: Path) -> None:
        rep = measure_drift(tmp_path / "absent", db_path=db_path, now=NOW)
        assert rep.in_sync and rep.derived_capsules == 0

    def test_in_sync_is_serialized(self) -> None:
        assert _report().model_dump()["in_sync"] is True


class TestAdjustmentEntries:
    def test_only_nonzero_dimensions_become_rows(self) -> None:
        rows = adjustment_entries(
            _report(drift_bytes=-7), workspace="default", org="default", actor="a"
        )
        assert [(r.metric, r.amount) for r in rows] == [(METRIC_BYTES, -7)]
        assert rows[0].attribution == "reconciliation"
        assert rows[0].ref == f"recon:{NOW.isoformat()}"

    def test_in_sync_report_yields_nothing(self) -> None:
        assert adjustment_entries(_report(), workspace="d", org="d", actor="a") == []


# --------------------------------------------------------------------------- #
# Reconcile — report / apply / refusal / audit
# --------------------------------------------------------------------------- #


class TestReconcile:
    def test_report_only_writes_no_ledger_row_and_audits(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        _capsule(capsule_dir, "legacy", 10)
        before = _ledger(db_path)
        res = reconcile(capsule_dir, db_path=db_path, now=NOW)
        assert not res.applied and res.rows_recorded == 0 and res.ref is None
        assert res.audited
        assert _ledger(db_path) == before
        (entry,) = _audit_entries(audit_log)
        assert entry["event_type"] == "usage.reconcile"
        assert entry["details"]["applied"] is False
        assert entry["details"]["drift_capsules"] == 1
        assert AuditLog(audit_log).verify() == []

    def test_apply_appends_signed_rows_to_default_only(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        _meter(db_path, "r1", _capsule(capsule_dir, "r1"), _TEAM_A)
        legacy = _capsule(capsule_dir, "legacy", 40)
        before = _ledger(db_path)

        res = reconcile(capsule_dir, apply=True, actor="alice", db_path=db_path, now=NOW)

        assert res.applied and res.rows_recorded == 2 and res.audited
        assert res.workspace == "default"
        after = _ledger(db_path)
        # Append-only: every pre-existing row is still there, unchanged.
        assert all(r in after for r in before)
        new = [r for r in after if r not in before]
        assert {(r["metric"], r["amount"]) for r in new} == {
            (METRIC_CAPSULES, 1),
            (METRIC_BYTES, legacy),
        }
        assert {r["workspace"] for r in new} == {"default"}
        assert {r["attribution"] for r in new} == {"reconciliation"}
        assert {r["ref"] for r in new} == {res.ref}
        assert {r["actor"] for r in new} == {"alice"}
        # team-a is untouched; drift is now closed.
        totals = usage.all_time_totals(db_path=db_path)
        assert totals["team-a"][METRIC_CAPSULES] == 1
        assert measure_drift(capsule_dir, db_path=db_path).in_sync
        (entry,) = _audit_entries(audit_log)
        assert entry["resource_id"] == res.ref
        assert entry["details"]["applied"] is True

    def test_second_apply_is_a_noop(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        _capsule(capsule_dir, "legacy", 5)
        reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)
        rows = _ledger(db_path)
        again = reconcile(capsule_dir, apply=True, db_path=db_path)
        assert not again.applied and again.report.in_sync
        assert _ledger(db_path) == rows
        assert len(_audit_entries(audit_log)) == 2

    def test_negative_drift_on_default_is_applied(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        size = _capsule(capsule_dir, "r1", 20)
        _meter(db_path, "r1", size)
        # Out-of-band deletion: the store lost the capsule, the ledger did not.
        for f in (capsule_dir / "r1").iterdir():
            f.unlink()
        (capsule_dir / "r1").rmdir()
        res = reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)
        assert res.applied
        assert usage.all_time_totals(db_path=db_path)["default"] == {
            METRIC_CAPSULES: 0,
            METRIC_BYTES: 0,
        }

    def test_negative_drift_owned_by_another_workspace_is_refused(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        _meter(db_path, "r1", 123, _TEAM_A)  # metered, but absent from the store
        before = _ledger(db_path)
        with pytest.raises(ReconciliationRefusedError, match="below zero"):
            reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)
        assert _ledger(db_path) == before
        assert _audit_entries(audit_log) == []

    def test_apply_when_in_sync_writes_nothing(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        res = reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)
        assert not res.applied and res.audited
        assert _ledger(db_path) == []

    def test_adjustment_uses_default_workspace_org(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        from novafabric.server import workspace_store

        workspace_store.ensure_default(db_path=db_path)
        _capsule(capsule_dir, "legacy")
        reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)
        assert {r["org"] for r in _ledger(db_path)} == {"default"}

    def test_report_only_audit_failure_degrades(
        self,
        db_path: Path,
        capsule_dir: Path,
        audit_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(AuditLog, "append", boom)
        res = reconcile(capsule_dir, db_path=db_path, now=NOW)
        assert res.audited is False

    def test_apply_audit_failure_is_loud(
        self,
        db_path: Path,
        capsule_dir: Path,
        audit_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise OSError("disk full")

        _capsule(capsule_dir, "legacy")
        monkeypatch.setattr(AuditLog, "append", boom)
        with pytest.raises(ReconciliationAuditError, match="recorded"):
            reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)
        assert len(_ledger(db_path)) == 2  # committed; the error says so


# --------------------------------------------------------------------------- #
# Reconcile — retention window (5a) and concurrent apply (5b)
# --------------------------------------------------------------------------- #

OLD = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _recon_rows(db: Path) -> list[dict[str, Any]]:
    return [r for r in _ledger(db) if r["attribution"] == "reconciliation"]


class TestReconcileRetentionWindow:
    def test_pruned_rollup_is_not_drift(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        old_size = _capsule(capsule_dir, "old", 30)
        usage.record_capsule_upload(
            run_id="old",
            size_bytes=old_size,
            attribution=_DEFAULT,
            actor="t",
            db_path=db_path,
            now=OLD,
        )
        # A write 32 months later finalizes 2024-01 and prunes it (> 24 months).
        _meter(db_path, "new", _capsule(capsule_dir, "new", 7))

        window = usage.all_time_totals(db_path=db_path)["default"]
        assert window[METRIC_CAPSULES] == 1  # enforcement window lost "old"
        life = usage.lifetime_totals(db_path=db_path)["default"]
        assert life[METRIC_CAPSULES] == 2

        assert measure_drift(capsule_dir, db_path=db_path, now=NOW).in_sync
        res = reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)
        assert not res.applied and _recon_rows(db_path) == []

    def test_monthly_prunes_never_rebook(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        # One upload per month over 30 months; reconcile --apply every month.
        for i in range(30):
            when = datetime(2024 + i // 12, i % 12 + 1, 10, tzinfo=timezone.utc)
            run_id = f"r{i:02d}"
            usage.record_capsule_upload(
                run_id=run_id,
                size_bytes=_capsule(capsule_dir, run_id, i),
                attribution=_DEFAULT,
                actor="t",
                db_path=db_path,
                now=when,
            )
            res = reconcile(capsule_dir, apply=True, db_path=db_path, now=when)
            assert not res.applied, f"prune booked as drift at {when:%Y-%m}"
        assert _recon_rows(db_path) == []

    def test_pruned_reconciliation_row_stays_neutral(
        self, db_path: Path, capsule_dir: Path, audit_log: Path
    ) -> None:
        _capsule(capsule_dir, "legacy", 11)
        first = reconcile(capsule_dir, apply=True, db_path=db_path, now=OLD)
        assert first.applied
        _meter(db_path, "new", _capsule(capsule_dir, "new"))  # prunes 2024-01
        assert measure_drift(capsule_dir, db_path=db_path, now=NOW).in_sync
        again = reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)
        assert not again.applied
        assert _recon_rows(db_path) == []  # 2024 rows pruned; nothing re-booked

    def test_carry_accumulates_and_counters_are_not_double_counted(self, db_path: Path) -> None:
        # Ledger retention longer than rollup retention (misconfiguration):
        # the carried period's counters must not resurface after the prune.
        kw = {"rollup_retention_months": 2, "ledger_retention_months": 6}
        for month, run_id in ((1, "a"), (2, "b"), (5, "c"), (6, "d")):
            usage.record_capsule_upload(
                run_id=run_id,
                size_bytes=10,
                attribution=_DEFAULT,
                actor="t",
                db_path=db_path,
                now=datetime(2026, month, 3, tzinfo=timezone.utc),
                **kw,
            )
        life = usage.lifetime_totals(db_path=db_path)["default"]
        assert life == {METRIC_CAPSULES: 4, METRIC_BYTES: 40}
        conn = usage.open_usage_db(db_path)
        try:
            carry = {
                r["metric"]: (r["total"], r["through"])
                for r in conn.execute("SELECT * FROM usage_pruned_totals")
            }
        finally:
            conn.close()
        assert carry[METRIC_CAPSULES] == (2, "2026-02")


class TestReconcileConcurrentApply:
    def test_interleaved_applies_book_drift_once(
        self,
        db_path: Path,
        capsule_dir: Path,
        audit_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Run B scans, run A applies fully, then B continues: B books nothing."""
        from novafabric.server import usage_reconcile

        legacy = _capsule(capsule_dir, "legacy", 50)
        real = usage_reconcile.measure_capsule_store
        state = {"nested": False}

        def scan_then_race(path: Path) -> Any:
            derived = real(path)
            if not state["nested"]:
                state["nested"] = True
                later = datetime(2026, 9, 15, 12, 0, 1, tzinfo=timezone.utc)
                inner = reconcile(capsule_dir, apply=True, db_path=db_path, now=later)
                assert inner.applied
            return derived

        monkeypatch.setattr(usage_reconcile, "measure_capsule_store", scan_then_race)
        outer = reconcile(capsule_dir, apply=True, db_path=db_path, now=NOW)

        assert not outer.applied and outer.report.in_sync
        rows = _recon_rows(db_path)
        assert {(r["metric"], r["amount"]) for r in rows} == {
            (METRIC_CAPSULES, 1),
            (METRIC_BYTES, legacy),
        }
        assert usage.lifetime_totals(db_path=db_path)["default"] == {
            METRIC_CAPSULES: 1,
            METRIC_BYTES: legacy,
        }

    def test_threaded_applies_book_drift_once(
        self,
        db_path: Path,
        capsule_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import threading

        from novafabric.server import usage_reconcile

        legacy = _capsule(capsule_dir, "legacy", 64)
        usage.open_usage_db(db_path).close()  # create tables up front
        monkeypatch.setattr(usage_reconcile, "_audit_run", lambda *_a, **_k: None)
        n = 6
        barrier = threading.Barrier(n)
        real = usage_reconcile.measure_capsule_store

        def scan_in_lockstep(path: Path) -> Any:
            # Every run has scanned the store before any run reaches the
            # ledger — the worst-case interleaving for a double apply.
            derived = real(path)
            barrier.wait(timeout=10)
            return derived

        monkeypatch.setattr(usage_reconcile, "measure_capsule_store", scan_in_lockstep)
        results: list[Any] = []
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                when = datetime(2026, 9, 15, 12, 0, i, tzinfo=timezone.utc)
                results.append(reconcile(capsule_dir, apply=True, db_path=db_path, now=when))
            except BaseException as exc:  # noqa: BLE001 — surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        assert sum(r.applied for r in results) == 1
        assert len(_recon_rows(db_path)) == 2
        assert usage.lifetime_totals(db_path=db_path)["default"] == {
            METRIC_CAPSULES: 1,
            METRIC_BYTES: legacy,
        }


# --------------------------------------------------------------------------- #
# Export — period ranges
# --------------------------------------------------------------------------- #


class TestPeriodsBetween:
    def test_single_period(self) -> None:
        assert periods_between("2026-09", "2026-09") == ["2026-09"]

    def test_year_wrap(self) -> None:
        assert periods_between("2025-11", "2026-02") == [
            "2025-11",
            "2025-12",
            "2026-01",
            "2026-02",
        ]

    @pytest.mark.parametrize(
        ("start", "end", "match"),
        [
            ("2026-13", "2026-13", "expected YYYY-MM"),
            ("2026-9", "2026-10", "expected YYYY-MM"),
            ("2026-05", "2026-04", "inverted"),
            ("2000-01", "2026-01", "maximum"),
        ],
    )
    def test_invalid_ranges_are_refused(self, start: str, end: str, match: str) -> None:
        with pytest.raises(InvalidPeriodRangeError, match=match):
            periods_between(start, end)

    def test_max_periods_is_inclusive(self) -> None:
        assert len(periods_between("2017-01", "2026-12")) == usage_export.MAX_PERIODS


# --------------------------------------------------------------------------- #
# Export — rows
# --------------------------------------------------------------------------- #


def _seed_two_periods(db: Path) -> None:
    aug = datetime(2026, 8, 10, tzinfo=timezone.utc)
    usage.record_capsule_upload(
        run_id="a1", size_bytes=10, attribution=_TEAM_A, actor="t", db_path=db, now=aug
    )
    usage.record_capsule_upload(
        run_id="d1", size_bytes=5, attribution=_DEFAULT, actor="t", db_path=db, now=aug
    )
    # First write of September finalizes August into rollups.
    usage.record_capsule_upload(
        run_id="a2", size_bytes=7, attribution=_TEAM_A, actor="t", db_path=db, now=NOW
    )


class TestChargebackRows:
    def test_final_and_provisional_rows_in_deterministic_order(self, db_path: Path) -> None:
        _seed_two_periods(db_path)
        rows = chargeback_rows("2026-08", "2026-09", db_path=db_path)
        keys = [(r.period, r.org, r.workspace, r.metric) for r in rows]
        assert keys == sorted(keys)
        aug = [r for r in rows if r.period == "2026-08"]
        sep = [r for r in rows if r.period == "2026-09"]
        assert {r.status for r in aug} == {"final"}
        assert all(r.finalized_at for r in aug)
        assert {r.status for r in sep} == {"provisional"}
        assert all(r.finalized_at is None for r in sep)
        assert {(r.workspace, r.metric, r.total) for r in sep} == {
            ("team-a", METRIC_CAPSULES, 1),
            ("team-a", METRIC_BYTES, 7),
        }
        assert {r.org for r in rows if r.workspace == "team-a"} == {"acme"}

    def test_filters(self, db_path: Path) -> None:
        _seed_two_periods(db_path)
        ws = chargeback_rows("2026-08", "2026-09", db_path=db_path, workspace="default")
        assert {r.workspace for r in ws} == {"default"} and ws
        org = chargeback_rows("2026-08", "2026-09", db_path=db_path, org="acme")
        assert {r.org for r in org} == {"acme"} and org

    def test_period_outside_range_excluded_and_empty_is_empty(self, db_path: Path) -> None:
        _seed_two_periods(db_path)
        assert {r.period for r in chargeback_rows("2026-09", "2026-09", db_path=db_path)} == {
            "2026-09"
        }
        assert chargeback_rows("2020-01", "2020-02", db_path=db_path) == []

    def test_export_is_read_only(self, db_path: Path) -> None:
        _seed_two_periods(db_path)
        before = _ledger(db_path)
        chargeback_rows("2026-08", "2026-09", db_path=db_path)
        assert _ledger(db_path) == before


# --------------------------------------------------------------------------- #
# Export — serialization
# --------------------------------------------------------------------------- #


def _row(**kw: object) -> ChargebackRow:
    base: dict[str, object] = {
        "period": "2026-09",
        "org": "acme",
        "workspace": "team-a",
        "metric": METRIC_BYTES,
        "total": 10,
        "status": "provisional",
    }
    base.update(kw)
    return ChargebackRow.model_validate(base)


class TestSerialization:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("=1+1", "'=1+1"),
            ("+cmd", "'+cmd"),
            ("-2", "'-2"),
            ("@SUM(A1)", "'@SUM(A1)"),
            ("\tx", "'\tx"),
            ("\rx", "'\rx"),
            ("\nx", "'\nx"),
            # Full-width look-alikes fold to ASCII triggers under NFKC.
            ("\uff1dcmd", "'\uff1dcmd"),
            ("\uff0bcmd", "'\uff0bcmd"),
            ("\uff0dcmd", "'\uff0dcmd"),
            ("\uff20SUM(A1)", "'\uff20SUM(A1)"),
            # Leading whitespace (incl. NBSP / ideographic space) then a trigger.
            (" =1+1", "' =1+1"),
            ("  @x", "'  @x"),
            ("\u00a0=1", "'\u00a0=1"),
            ("\u3000\uff1d1", "'\u3000\uff1d1"),
            ("\n\t=1", "'\n\t=1"),
            # Benign text — untouched, including whitespace without a trigger.
            ("team-a", "team-a"),
            (" team a", " team a"),
            ("\uff54eam", "\uff54eam"),
            ("", ""),
        ],
    )
    def test_safe_cell(self, value: str, expected: str) -> None:
        assert safe_cell(value) == expected

    def test_csv_is_rfc4180_and_injection_safe(self) -> None:
        rows = [
            _row(workspace='=HYPERLINK("http://evil","x")', total=-3),
            _row(workspace="a,b", status="final", finalized_at="2026-09-01T00:00:00"),
        ]
        text = to_csv(rows)
        assert text.startswith(",".join(usage_export.COLUMNS) + "\r\n")
        assert text.endswith("\r\n") and "\n" not in text.replace("\r\n", "")
        parsed = list(csv.reader(io.StringIO(text, newline="")))
        assert parsed[1][2] == '\'=HYPERLINK("http://evil","x")'
        assert parsed[1][4] == "-3"  # integer cells are never prefixed
        assert parsed[1][6] == ""  # None -> empty cell
        assert parsed[2][2] == "a,b"
        assert '"a,b"' in text

    def test_csv_neutralizes_unicode_and_whitespace_triggers(self) -> None:
        rows = [
            _row(workspace="\uff1dHYPERLINK(1)"),
            _row(workspace=" \n=cmd", org="\uff20x"),
        ]
        text = to_csv(rows)
        assert text.endswith("\r\n")
        parsed = list(csv.reader(io.StringIO(text, newline="")))
        assert parsed[1][2] == "'\uff1dHYPERLINK(1)"
        assert parsed[2][1] == "'\uff20x"
        # Embedded LF survives RFC 4180 quoting round-trip, prefix intact.
        assert parsed[2][2] == "' \n=cmd"

    def test_empty_csv_is_header_only(self) -> None:
        assert to_csv([]) == ",".join(usage_export.COLUMNS) + "\r\n"

    def test_ndjson_is_sorted_keys_one_object_per_line(self) -> None:
        text = to_ndjson([_row(), _row(metric=METRIC_CAPSULES, total=1)])
        lines = text.splitlines()
        assert len(lines) == 2 and text.endswith("\n")
        obj = json.loads(lines[0])
        assert list(obj) == sorted(obj)
        assert obj["workspace"] == "team-a" and obj["total"] == 10

    def test_render_dispatch_and_determinism(self, db_path: Path) -> None:
        _seed_two_periods(db_path)
        rows = chargeback_rows("2026-08", "2026-09", db_path=db_path)
        again = chargeback_rows("2026-08", "2026-09", db_path=db_path)
        assert render(rows, "csv") == render(again, "csv") == to_csv(rows)
        assert render(rows, "ndjson") == to_ndjson(again)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cli(*args: str, tmp: Path) -> Result:
    return runner.invoke(app, ["server", "usage", *args, "--config", str(tmp / "no-such.yaml")])


class TestCli:
    def test_help_lists_both_subcommands(self) -> None:
        res = runner.invoke(app, ["server", "usage", "--help"])
        assert res.exit_code == 0
        assert "reconcile" in res.output and "export" in res.output

    def test_reconcile_report_text(
        self, db_path: Path, capsule_dir: Path, audit_log: Path, tmp_path: Path
    ) -> None:
        _capsule(capsule_dir, "legacy", 3)
        res = _cli(
            "reconcile",
            "--db-path",
            str(db_path),
            "--capsule-dir",
            str(capsule_dir),
            tmp=tmp_path,
        )
        assert res.exit_code == 0, res.output
        assert "drift   : +1 capsules" in res.output
        assert "Report only" in res.output
        assert _ledger(db_path) == []

    def test_reconcile_apply_json(
        self, db_path: Path, capsule_dir: Path, audit_log: Path, tmp_path: Path
    ) -> None:
        _capsule(capsule_dir, "legacy", 3)
        res = _cli(
            "reconcile",
            "--apply",
            "--json",
            "--actor",
            "ops",
            "--db-path",
            str(db_path),
            "--capsule-dir",
            str(capsule_dir),
            tmp=tmp_path,
        )
        assert res.exit_code == 0, res.output
        body = json.loads(res.output)
        assert body["applied"] is True and body["rows_recorded"] == 2
        assert {r["actor"] for r in _ledger(db_path)} == {"ops"}

    def test_reconcile_apply_text_and_in_sync(
        self, db_path: Path, capsule_dir: Path, audit_log: Path, tmp_path: Path
    ) -> None:
        _capsule(capsule_dir, "legacy")
        args = ("--db-path", str(db_path), "--capsule-dir", str(capsule_dir))
        res = _cli("reconcile", "--apply", *args, tmp=tmp_path)
        assert "Applied: 2 adjustment row(s)" in res.output
        res = _cli("reconcile", *args, tmp=tmp_path)
        assert "In sync" in res.output

    def test_reconcile_warns_when_audit_fails(
        self,
        db_path: Path,
        capsule_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise OSError("ro")

        monkeypatch.setattr(AuditLog, "append", boom)
        res = _cli(
            "reconcile",
            "--db-path",
            str(db_path),
            "--capsule-dir",
            str(capsule_dir),
            tmp=tmp_path,
        )
        assert res.exit_code == 0
        assert "audit entry could not be appended" in res.output

    def test_reconcile_refusal_exits_1(
        self, db_path: Path, capsule_dir: Path, audit_log: Path, tmp_path: Path
    ) -> None:
        _meter(db_path, "r1", 9, _TEAM_A)
        res = _cli(
            "reconcile",
            "--apply",
            "--db-path",
            str(db_path),
            "--capsule-dir",
            str(capsule_dir),
            tmp=tmp_path,
        )
        assert res.exit_code == 1
        assert "below zero" in res.output

    def test_invalid_config_exits_2(self, tmp_path: Path, db_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("usage:\n  rollup_retention_months: 0\n")
        res = runner.invoke(app, ["server", "usage", "reconcile", "--config", str(bad)])
        assert res.exit_code == 2
        assert "Invalid server config" in res.output

    def test_export_csv_stdout(self, db_path: Path, tmp_path: Path) -> None:
        _seed_two_periods(db_path)
        res = _cli(
            "export",
            "--from",
            "2026-08",
            "--to",
            "2026-09",
            "--db-path",
            str(db_path),
            tmp=tmp_path,
        )
        assert res.exit_code == 0, res.output
        assert res.output.startswith("period,org,workspace,metric,total,status")
        # Result.output normalizes newlines; the raw bytes keep RFC 4180 CRLF.
        assert b"\r\n" in res.stdout_bytes

    def test_export_ndjson_to_file(self, db_path: Path, tmp_path: Path) -> None:
        _seed_two_periods(db_path)
        out = tmp_path / "cb.ndjson"
        res = _cli(
            "export",
            "--from",
            "2026-08",
            "--format",
            "ndjson",
            "--workspace",
            "team-a",
            "-o",
            str(out),
            "--db-path",
            str(db_path),
            tmp=tmp_path,
        )
        assert res.exit_code == 0, res.output
        lines = out.read_text().splitlines()
        assert lines and all(json.loads(ln)["workspace"] == "team-a" for ln in lines)
        assert f"to {out}" in res.output

    def test_export_default_period_is_current(self, db_path: Path, tmp_path: Path) -> None:
        res = _cli("export", "--db-path", str(db_path), tmp=tmp_path)
        assert res.exit_code == 0
        assert res.stdout_bytes == (",".join(usage_export.COLUMNS) + "\r\n").encode()

    @pytest.mark.parametrize(
        "args",
        [
            ("--format", "xlsx"),
            ("--from", "2026-13"),
            ("--from", "2026-09", "--to", "2026-01"),
        ],
    )
    def test_export_bad_input_exits_2(
        self, db_path: Path, tmp_path: Path, args: tuple[str, ...]
    ) -> None:
        res = _cli("export", *args, "--db-path", str(db_path), tmp=tmp_path)
        assert res.exit_code == 2
