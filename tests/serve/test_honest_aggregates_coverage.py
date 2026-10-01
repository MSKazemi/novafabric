"""ADR-0234 D2 applied beyond ``cost-summary`` — every run aggregate the
dashboard serves carries a verdict, and refuses rather than render a number
its data cannot support.

Acceptance criteria (spec ``honest-aggregates-v0`` I1–I3, extended):

* ``/api/stats`` refuses and nulls its run counts when the runs index holds
  fewer runs than the capsules on disk (a partial index undercounts);
* ``/api/cost/report`` refuses — ``cost_usd: null``, not ``0.0`` — when every
  call in the window is unpriced, and states the unpriced share otherwise;
* the run-aggregate reports (cost-burn, throughput, executive-summary) refuse
  on a partial index as JSON, as a 409 for CSV, and as a 409 for the HTML/PDF
  artifact, so no exported file carries the numbers without the refusal;
* a success rate over zero runs is ``null`` (undefined), never ``0.0``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.registry.runs_cache import ensure_runs_cache, upsert_run  # noqa: E402
from novafabric.registry.store import get_connection, init_schema  # noqa: E402
from novafabric.serve.app import create_app  # noqa: E402

TOKEN = "testtoken-0123456789"
H = {"host": "127.0.0.1:4321"}


def _index(db: Path, run_ids: list[str]) -> None:
    conn = get_connection(db)
    init_schema(conn)
    ensure_runs_cache(conn)
    for rid in run_ids:
        upsert_run(
            conn,
            {
                "run_id": rid,
                "status": "success",
                "created_at": "2026-07-14T09:00:00Z",
                "duration_ms": 10.0,
                "model_call_count": 1,
                "tool_call_count": 0,
                "command": ["python"],
            },
        )
    conn.commit()
    conn.close()


def _capsule(base: Path, rid: str) -> None:
    (base / rid).mkdir(parents=True)
    (base / rid / "capsule.yaml").write_text(f"run_id: {rid}\n")


@pytest.fixture
def partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Four capsules on disk, two in the index."""
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
    base = tmp_path / "capsules"
    base.mkdir()
    for rid in ("r1", "r2", "r3", "r4"):
        _capsule(base, rid)
    db = tmp_path / "registry.db"
    _index(db, ["r1", "r2"])
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    yield TestClient(app)


@pytest.fixture
def complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
    base = tmp_path / "capsules"
    base.mkdir()
    for rid in ("r1", "r2"):
        _capsule(base, rid)
    db = tmp_path / "registry.db"
    _index(db, ["r1", "r2"])
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    yield TestClient(app)


def _get(c: TestClient, path: str, **params: str) -> object:
    return c.get(path, params={"token": TOKEN, **params}, headers=H)


# ---------------------------------------------------------------------------
# /api/stats
# ---------------------------------------------------------------------------


def test_stats_refuses_and_nulls_counts_on_a_partial_index(partial: TestClient) -> None:
    body = _get(partial, "/api/stats").json()  # type: ignore[attr-defined]
    agg = body["aggregate"]
    assert agg["computable"] is False
    assert agg["condition"] == "truncated_source"
    assert agg["notes"] == {"indexed": 2, "on_disk": 4}
    assert body["run_count"] is None
    assert body["failed_run_count"] is None
    assert body["passed_run_count"] is None
    # Asset counts are not run aggregates and are unaffected.
    assert isinstance(body["asset_count"], int)


def test_stats_is_computable_on_a_complete_index(complete: TestClient) -> None:
    body = _get(complete, "/api/stats").json()  # type: ignore[attr-defined]
    assert body["aggregate"] == {
        "computable": True,
        "value": 2,
        "notes": {"source": "runs_cache"},
    }
    assert body["run_count"] == 2


# ---------------------------------------------------------------------------
# Run-aggregate reports — JSON, CSV, and the exported artifact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("report", ["cost-burn", "throughput", "executive-summary"])
def test_reports_refuse_on_a_partial_index_in_every_format(
    partial: TestClient, report: str
) -> None:
    js = _get(partial, f"/api/reports/{report}").json()  # type: ignore[attr-defined]
    assert js["aggregate"]["condition"] == "truncated_source"
    assert js["rows"] == []

    csv = _get(partial, f"/api/reports/{report}", format="csv")
    assert csv.status_code == 409  # type: ignore[attr-defined]
    assert csv.json()["aggregate"]["condition"] == "truncated_source"  # type: ignore[attr-defined]

    art = _get(partial, f"/api/reports/{report}/export", format="html")
    assert art.status_code == 409  # type: ignore[attr-defined]


@pytest.mark.parametrize("report", ["cost-burn", "throughput", "executive-summary"])
def test_reports_are_computable_on_a_complete_index(complete: TestClient, report: str) -> None:
    js = _get(complete, f"/api/reports/{report}").json()  # type: ignore[attr-defined]
    assert js["aggregate"]["computable"] is True
    assert js["rows"]


def test_a_non_aggregate_report_artifact_is_not_guarded(partial: TestClient) -> None:
    r = _get(partial, "/api/reports/run-history/export", format="html")
    assert r.status_code == 200  # type: ignore[attr-defined]


def test_a_success_rate_over_zero_runs_is_undefined(tmp_path: Path) -> None:
    from novafabric.serve.reports import report_executive_summary

    base = tmp_path / "empty"
    base.mkdir()
    _, rows = report_executive_summary(base)
    assert rows[0]["total_runs"] == 0
    assert rows[0]["success_rate_pct"] is None


# ---------------------------------------------------------------------------
# /api/cost/report — unpriced is not free
# ---------------------------------------------------------------------------


def _calls(base: Path, rid: str, models: list[str]) -> None:
    cdir = base / rid
    cdir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    recs = [
        {
            "gen_ai.request.model": m,
            "gen_ai.response.model": m,
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 50,
            "started_at": now,
        }
        for m in models
    ]
    (cdir / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _cost_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    monkeypatch.delenv("NOVA_CLICKHOUSE_URL", raising=False)
    monkeypatch.setenv("NOVA_EVIDENCE_DUCKDB_PATH", str(tmp_path / "absent" / "e.duckdb"))
    base = tmp_path / "capsules"
    base.mkdir()
    app = create_app(token=TOKEN, capsule_dir=base, db_path=tmp_path / "r.db", static_dir=None)
    return TestClient(app), base


def test_an_all_unpriced_window_refuses_cost_and_keeps_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, base = _cost_client(tmp_path, monkeypatch)
    _calls(base, "run-a", ["no-such-model-xyz"])
    body = _get(c, "/api/cost/report").json()  # type: ignore[attr-defined]
    assert body["aggregate"]["computable"] is False
    assert body["aggregate"]["condition"] == "absent_contributor"
    assert "value" not in body["aggregate"]
    assert body["totals"]["cost_usd"] is None
    assert body["totals"]["input_tokens"] == 100  # tokens are exact either way


def test_a_partly_unpriced_window_is_a_stated_lower_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, base = _cost_client(tmp_path, monkeypatch)
    _calls(base, "run-a", ["gpt-4o-mini", "no-such-model-xyz"])
    body = _get(c, "/api/cost/report").json()  # type: ignore[attr-defined]
    agg = body["aggregate"]
    assert agg["computable"] is True
    assert agg["notes"]["unpriced_models"] == ["no-such-model-xyz"]
    assert agg["notes"]["cost_is_lower_bound"] is True
    assert agg["value"] == body["totals"]["cost_usd"] > 0


def test_an_empty_window_is_a_measured_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = _cost_client(tmp_path, monkeypatch)
    body = _get(c, "/api/cost/report").json()  # type: ignore[attr-defined]
    assert body["aggregate"]["computable"] is True
    assert body["totals"]["cost_usd"] == 0
