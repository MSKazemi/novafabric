# Dashboard scale gate — thresholds and how to run it

**Status:** works today (gate + thresholds shipped with ADR-0199).
**Per:** [ADR-0199](../../design/adr/0199-dashboard-scale-posture.md) — the
dashboard's scale contract (bounded queries, keyset cursors, watermark caching,
honest truncation).

The dashboard's latency posture is enforced by a seeded benchmark, not by hope:
`tests/bench/test_dashboard_scale_gate.py` seeds a deterministic **100 000-row
`runs_cache`** (spread over ~90 days) plus a **100 000-line dashboard audit
log**, mounts the serve app in-process (TestClient — measures query cost,
excludes network), and asserts p95 over 30 requests per endpoint.

## Thresholds (p95, 100K rows, in-process)

| Endpoint | Threshold | What it proves |
|---|---|---|
| `GET /api/runs?limit=100` (first page) | 100 ms | indexed page reads |
| `GET /api/runs/search?limit=100` | 100 ms | keyset search page |
| `GET /api/analytics/summary` (90-day window) | 250 ms | SQL day-bucket aggregation (expression index) |
| `GET /api/reports/throughput` | 300 ms | windowed GROUP BY pushdown (S2) |
| `GET /api/reports/executive-summary` | 300 ms | whole-window totals in one SQL row |
| `GET /api/reports/run-history?limit=1000` | 300 ms | bounded row-level page |
| same, ~50 pages deep via `cursor=` | 300 ms | keyset stays O(page), never O(offset) |
| `GET /api/audit?limit=200` (100K-line log) | 100 ms | reverse-block tail reads (S1) |

Changing a threshold is a release decision: update this table, the test, and
the "Scale characteristics" section of [`docs/dashboard.md`](../dashboard.md)
in the same PR (docs honesty rule).

## Running it

```bash
# Local (skipped by default so `make test-fast` is unaffected):
NOVA_DASHBOARD_SCALE=1 uv run pytest tests/bench/test_dashboard_scale_gate.py -v

# CI: the `dashboard-scale` job in .github/workflows/nightly-scale-gates.yml
# runs it nightly (03:00 UTC) and on workflow_dispatch.
```

The seeder (`seed_runs_cache`, `seed_audit_log` in the test module) is
importable for ad-hoc experiments — e.g. seeding a scratch registry to eyeball
dashboard behavior at 100K runs:

```bash
NOVA_DASHBOARD_SCALE=1 uv run python -c "
from pathlib import Path
from tests.bench.test_dashboard_scale_gate import seed_runs_cache
seed_runs_cache(Path('/tmp/scale-registry.db'))
"
nova serve --experimental --db-path /tmp/scale-registry.db
```

## What the gate does NOT cover (honest limits)

- **Network + browser rendering** — TestClient is in-process; wire latency and
  React rendering are outside the gate. The frontend's own guards are
  virtualized tables and code-split tabs.
- **Postgres tier** — the `/v0` API's keyset path is exercised by the existing
  `postgres-scale` nightly job; this gate is deliberately SQLite-only to prove
  the *local-first* tier holds at 100K rows.
- **1M-row tier** — ADR-0199 asserts SQLite must remain sufficient to ~1M rows;
  the revisit trigger for a columnar path (DuckDB/Parquet snapshot) is this
  gate failing after being re-seeded at 1M. Bump `N_RUNS` locally to probe.
