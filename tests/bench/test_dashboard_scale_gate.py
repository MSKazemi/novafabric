"""Dashboard scale gate — p95 latency thresholds at a 100K-row store (ADR-0199 §6).

Seeds a deterministic 100K-row ``runs_cache`` (created_at spread over ~90
days) plus a 100K-line dashboard audit log, mounts the serve app in-process
via TestClient (measures query cost, excludes network), and asserts p95 over
30 requests per endpoint against the thresholds recorded in
``docs/ops/dashboard-scale.md``.

Gated behind ``NOVA_DASHBOARD_SCALE=1`` so ``make test-fast`` is unaffected;
enforced by the nightly ``dashboard-scale`` CI job
(.github/workflows/nightly-scale-gates.yml). Regressing a threshold blocks
release the same way the coverage gate does (ADR-0199).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from bench.slo import slo_value

pytestmark = pytest.mark.skipif(
    os.environ.get("NOVA_DASHBOARD_SCALE") != "1",
    reason="scale gate — set NOVA_DASHBOARD_SCALE=1 (nightly CI tier)",
)

TOKEN = "scaletoken"
H = {"host": "127.0.0.1:4321"}

N_RUNS = 100_000
N_AUDIT_LINES = 100_000
ROUNDS = 30

# p95 thresholds in seconds — see docs/ops/dashboard-scale.md before changing.
# The numbers live in the SLO catalog (ADR-0248, slo_catalog.toml) so the
# published claim and the gate cannot disagree.
_GATE_NAMES = (
    "runs_page",
    "runs_search_page",
    "analytics_summary",
    "report_throughput",
    "report_executive_summary",
    "report_run_history_page",
    "report_run_history_deep_keyset",
    "audit_tail",
)
THRESHOLDS = {
    name: slo_value(f"dashboard.{name.replace('_', '-')}.p95") for name in _GATE_NAMES
}


def seed_runs_cache(db: Path, n: int = N_RUNS) -> None:
    """Deterministic synthetic runs: created_at spread over ~90 days."""
    from novafabric.registry.runs_cache import ensure_runs_cache

    con = sqlite3.connect(str(db))
    ensure_runs_cache(con)
    rows = []
    for i in range(n):
        day = 1 + (i % 90)
        month = 4 + (day - 1) // 30  # 2026-04..2026-06
        dom = 1 + (day - 1) % 30
        status = "success" if i % 5 else "failure"
        rows.append(
            (
                f"run-{i:07d}",
                status,
                f"2026-{month:02d}-{dom:02d}T{i % 24:02d}:{i % 60:02d}:00Z",
                None,
                float(50 + (i % 1000)),
                0 if status == "success" else 1,
                i % 7,
                i % 3,
                0,
                json.dumps([f"agent-{i % 25}", "task"]),
                "0.63.0",
                None,
            )
        )
    con.executemany(
        "INSERT OR REPLACE INTO runs_cache VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    con.commit()
    con.close()


def seed_audit_log(path: Path, n: int = N_AUDIT_LINES) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(
                json.dumps(
                    {
                        "audit_id": f"a-{i:07d}",
                        "ts": f"2026-06-{1 + i % 28:02d}T00:00:00Z",
                        "action": ["promote", "eval", "redact"][i % 3],
                        "args": {"i": i},
                        "cli_equivalent": "nova …",
                        "actor_token_fp": "deadbeef",
                        "result": "ok",
                    }
                )
                + "\n"
            )


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    tmp = tmp_path_factory.mktemp("dash-scale")
    db = tmp / "registry.db"
    seed_runs_cache(db)
    audit_file = tmp / "dashboard-audit.jsonl"
    seed_audit_log(audit_file)
    os.environ["NOVAFABRIC_DASHBOARD_AUDIT_FILE"] = str(audit_file)
    try:
        from novafabric.serve.app import create_app

        caps = tmp / "capsules"
        caps.mkdir()
        app = create_app(token=TOKEN, capsule_dir=caps, db_path=db, static_dir=None)
        with TestClient(app) as c:
            yield c
    finally:
        os.environ.pop("NOVAFABRIC_DASHBOARD_AUDIT_FILE", None)


def _p95(client: TestClient, url: str, rounds: int = ROUNDS) -> float:
    # warmup
    r = client.get(url, headers=H)
    assert r.status_code == 200, f"{url} -> {r.status_code}: {r.text[:200]}"
    samples = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        client.get(url, headers=H)
        samples.append(time.perf_counter() - t0)
    samples.sort()
    return samples[min(len(samples) - 1, int(0.95 * len(samples)))]


def _assert_gate(name: str, p95: float) -> None:
    limit = THRESHOLDS[name]
    assert p95 < limit, (
        f"{name}: p95 {p95 * 1000:.1f} ms exceeds {limit * 1000:.0f} ms "
        f"at {N_RUNS} rows — ADR-0199 gate; see docs/ops/dashboard-scale.md"
    )


def test_runs_first_page(client: TestClient) -> None:
    _assert_gate("runs_page", _p95(client, f"/api/runs?token={TOKEN}&limit=100"))


def test_runs_search_page(client: TestClient) -> None:
    _assert_gate(
        "runs_search_page",
        _p95(client, f"/api/runs/search?token={TOKEN}&limit=100"),
    )


def test_analytics_summary_window(client: TestClient) -> None:
    _assert_gate(
        "analytics_summary",
        _p95(
            client,
            f"/api/analytics/summary?token={TOKEN}&since=2026-04-01&until=2026-06-30",
        ),
    )


def test_report_throughput(client: TestClient) -> None:
    _assert_gate(
        "report_throughput", _p95(client, f"/api/reports/throughput?token={TOKEN}")
    )


def test_report_executive_summary(client: TestClient) -> None:
    _assert_gate(
        "report_executive_summary",
        _p95(client, f"/api/reports/executive-summary?token={TOKEN}"),
    )


def test_report_run_history_first_page(client: TestClient) -> None:
    _assert_gate(
        "report_run_history_page",
        _p95(client, f"/api/reports/run-history?token={TOKEN}&limit=1000"),
    )


def test_report_run_history_deep_keyset(client: TestClient) -> None:
    # Walk ~50 pages in, then measure the deep page — keyset stays O(page).
    cursor = None
    for _ in range(50):
        url = f"/api/reports/run-history?token={TOKEN}&limit=1000"
        if cursor:
            url += f"&cursor={cursor}"
        r = client.get(url, headers=H)
        assert r.status_code == 200
        cursor = r.json()["next_cursor"]
        if cursor is None:
            break
    assert cursor is not None, "expected a deep cursor at 100K rows"
    _assert_gate(
        "report_run_history_deep_keyset",
        _p95(
            client,
            f"/api/reports/run-history?token={TOKEN}&limit=1000&cursor={cursor}",
            rounds=10,
        ),
    )


def test_audit_tail_bounded(client: TestClient) -> None:
    _assert_gate("audit_tail", _p95(client, f"/api/audit?token={TOKEN}&limit=200"))
