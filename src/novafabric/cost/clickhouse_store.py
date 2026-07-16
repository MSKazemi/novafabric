"""ClickHouse-backed cost aggregation store.

Reads model call token counts from capsule model-calls.jsonl files,
writes cost events to ClickHouse, and queries aggregated cost reports.
cap-002 / ADR-0066.

Schema changes in this version:
- Main table ``nova.cost_events`` remains ``MergeTree`` (raw events).
- New materialized view ``nova.cost_by_model_mv`` uses
  ``AggregatingMergeTree`` for per-tenant/model/date aggregates, allowing
  efficient incremental computation and low-latency ``sumMerge`` queries.
- New ``query_cost_report()`` queries the MV for the CLI/dashboard cost report.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from novafabric.cost.interceptor import CostInterceptor

log = logging.getLogger(__name__)

_CAPSULE_SUFFIX = "/model-calls.jsonl"


def _parse_url(url: str) -> dict[str, Any]:
    """Parse NOVA_CLICKHOUSE_URL into clickhouse_connect kwargs."""
    p = urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 8123,
        "username": p.username or "default",
        "password": p.password or "",
        "database": (p.path or "/nova").lstrip("/") or "nova",
    }


# ---------------------------------------------------------------------------
# DDL — raw events table + AggregatingMergeTree materialized view
# ---------------------------------------------------------------------------

_DDL_DATABASE = "CREATE DATABASE IF NOT EXISTS nova;"

_DDL_COST_EVENTS = """\
CREATE TABLE IF NOT EXISTS nova.cost_events (
    run_id        String,
    capsule_id    String,
    model_call_id String,
    tenant_id     String DEFAULT 'default',
    model_id      String,
    provider      String,
    input_tokens  UInt64,
    output_tokens UInt64,
    cached_tokens UInt64,
    cost_usd      Float64,
    recorded_at   DateTime DEFAULT now()
) ENGINE = MergeTree()
ORDER BY (tenant_id, recorded_at, run_id, model_call_id);
"""

# Materialized view using AggregatingMergeTree for incremental aggregation.
# Data flows: nova.cost_events → nova.cost_by_model_mv (auto-populated).
_DDL_COST_MV = """\
CREATE MATERIALIZED VIEW IF NOT EXISTS nova.cost_by_model_mv
ENGINE = AggregatingMergeTree()
ORDER BY (tenant_id, model_id, date)
POPULATE
AS SELECT
    tenant_id,
    model_id,
    toDate(recorded_at)                       AS date,
    sumState(cost_usd)                        AS total_usd_state,
    sumState(toFloat64(input_tokens))         AS prompt_tokens_state,
    sumState(toFloat64(output_tokens))        AS completion_tokens_state,
    countState()                              AS run_count_state
FROM nova.cost_events
GROUP BY tenant_id, model_id, toDate(recorded_at);
"""

_DDL_STATEMENTS = [_DDL_DATABASE, _DDL_COST_EVENTS, _DDL_COST_MV]

# Incremental migrations: columns added after the initial schema.
# Each entry is idempotent — ADD COLUMN IF NOT EXISTS is safe to re-run.
_MIGRATIONS: list[str] = [
    "ALTER TABLE nova.cost_events ADD COLUMN IF NOT EXISTS tenant_id String DEFAULT 'default';",
    "ALTER TABLE nova.cost_events ADD COLUMN IF NOT EXISTS capsule_id String DEFAULT '';",
]


def ensure_schema() -> None:
    """Create tables/views and apply incremental migrations.

    Called once at nova serve startup when NOVA_CLICKHOUSE_URL is set.
    Safe to call multiple times — all statements are idempotent.
    Logs a warning and returns on any error so serve starts regardless.
    """
    try:
        client = _get_client()
        for stmt in _MIGRATIONS:
            stmt = stmt.strip()
            if stmt:
                client.command(stmt)
        log.info("novafabric: ClickHouse schema verified / migrated OK")
    except Exception as exc:
        log.warning("novafabric: ClickHouse schema check failed (non-fatal): %s", exc)


def _get_client() -> Any:
    import clickhouse_connect  # no stubs (mypy override in pyproject)

    url = os.environ.get("NOVA_CLICKHOUSE_URL", "")
    if not url:
        raise RuntimeError("NOVA_CLICKHOUSE_URL not set")
    kwargs = _parse_url(url)
    client = clickhouse_connect.get_client(**kwargs)
    # Ensure the database, table, and MV exist; all CREATE IF NOT EXISTS.
    for stmt in _DDL_STATEMENTS:
        stmt = stmt.strip()
        if stmt:
            client.command(stmt)
    return client


def ingest_capsule(run_id: str, capsule_dir: Path, tenant_id: str = "default") -> int:
    """Read model-calls.jsonl from a capsule dir and push events to ClickHouse.

    Returns the number of rows inserted.  Skips runs already present in
    ClickHouse (idempotent via deduplication on run_id + model_call_id).
    """
    mcalls_path = capsule_dir / "model-calls.jsonl"
    if not mcalls_path.exists():
        return 0

    client = _get_client()

    # Check which model_call_ids are already in ClickHouse for this run
    existing: set[str] = set()
    try:
        result = client.query(
            "SELECT model_call_id FROM nova.cost_events WHERE run_id = {run_id:String}",
            parameters={"run_id": run_id},
        )
        existing = {row[0] for row in result.result_rows}
    except Exception:
        pass  # table might not exist yet; insertions will create it

    rows: list[tuple[Any, ...]] = []
    with mcalls_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            call_id: str = ev.get("model_call_id", "")
            if call_id in existing:
                continue

            model: str = ev.get("gen_ai.response.model") or ev.get(
                "gen_ai.request.model", ""
            )
            provider: str = ev.get("gen_ai.system", "unknown")
            input_tokens = int(ev.get("gen_ai.usage.input_tokens", 0))
            output_tokens = int(ev.get("gen_ai.usage.output_tokens", 0))
            # ADR-0132: the optional nova.usage block carries the per-type
            # breakdown; cached_tokens feeds the (pre-existing) column.
            # The column is non-nullable, so unknown is stored as 0 here —
            # the capsule record stays the source of truth for absent vs zero.
            nova_usage = ev.get("nova.usage")
            cached_tokens = 0
            if isinstance(nova_usage, dict):
                raw_cached = nova_usage.get("cached_tokens")
                if isinstance(raw_cached, int) and not isinstance(raw_cached, bool):
                    cached_tokens = max(raw_cached, 0)
            # Compute estimated cost
            cost_usd = CostInterceptor._estimate_cost(
                model, input_tokens, output_tokens
            )
            capsule_id: str = ev.get("capsule_id", run_id)

            rows.append(
                (
                    run_id,
                    capsule_id,
                    call_id,
                    tenant_id,
                    model,
                    provider,
                    input_tokens,
                    output_tokens,
                    cached_tokens,
                    cost_usd,
                )
            )

    if not rows:
        return 0

    client.insert(
        "nova.cost_events",
        rows,
        column_names=[
            "run_id",
            "capsule_id",
            "model_call_id",
            "tenant_id",
            "model_id",
            "provider",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "cost_usd",
        ],
    )
    log.info("cost: ingested %d model-call rows for run %s", len(rows), run_id)
    return len(rows)


def ingest_all_capsules(
    novafabric_home: Path, tenant_id: str = "default"
) -> dict[str, int]:
    """Scan all capsule directories and ingest any not yet in ClickHouse.

    Returns a mapping of run_id → rows_inserted (0 = already up to date).
    """
    capsules_root = novafabric_home / "capsules"
    if not capsules_root.is_dir():
        return {}

    results: dict[str, int] = {}
    for capsule_dir in sorted(capsules_root.iterdir()):
        if not capsule_dir.is_dir():
            continue
        run_id = capsule_dir.name
        try:
            n = ingest_capsule(run_id, capsule_dir, tenant_id=tenant_id)
            results[run_id] = n
        except Exception as exc:
            log.warning("cost: failed to ingest run %s: %s", run_id, exc)
            results[run_id] = -1
    return results


def cost_report(
    run_id: str | None = None,
    days: int = 7,
) -> dict[str, Any]:
    """Query ClickHouse for aggregated cost data.

    Returns a dict matching the /api/cost/report response schema.
    Queries the raw ``nova.cost_events`` table.
    """
    client = _get_client()

    base_filter = "recorded_at >= now() - toIntervalDay(toInt32({days:Int32}))"
    params: dict[str, Any] = {"days": days}

    if run_id:
        base_filter += " AND run_id = {run_id:String}"
        params["run_id"] = run_id

    totals_sql = f"""
        SELECT
            sum(input_tokens)  AS total_input,
            sum(output_tokens) AS total_output,
            sum(cached_tokens) AS total_cached,
            sum(cost_usd)      AS total_cost
        FROM nova.cost_events
        WHERE {base_filter}
    """
    by_model_sql = f"""
        SELECT
            model_id,
            provider,
            sum(input_tokens)  AS input_tokens,
            sum(output_tokens) AS output_tokens,
            sum(cached_tokens) AS cached_tokens,
            sum(cost_usd)      AS cost_usd,
            count()            AS calls
        FROM nova.cost_events
        WHERE {base_filter}
        GROUP BY model_id, provider
        ORDER BY cost_usd DESC
        LIMIT 50
    """

    totals_res = client.query(totals_sql, parameters=params)
    totals_row = (
        totals_res.result_rows[0] if totals_res.result_rows else (0, 0, 0, 0.0)
    )

    by_model_res = client.query(by_model_sql, parameters=params)
    by_model = [
        {
            "model_id": row[0],
            "provider": row[1],
            "input_tokens": row[2],
            "output_tokens": row[3],
            "cached_tokens": row[4],
            "cost_usd": round(float(row[5]), 6),
            "calls": row[6],
        }
        for row in by_model_res.result_rows
    ]

    return {
        "ok": True,
        "backend": "clickhouse",
        "run_id": run_id,
        "days": days,
        "totals": {
            "input_tokens": int(totals_row[0]),
            "completion_tokens": int(totals_row[1]),
            # ADR-0132: cached (prompt-cache read) usage type in the report.
            # ClickHouse stores unknown as 0 (non-nullable column); the capsule
            # nova.usage / usage_totals blocks remain the absent-vs-zero truth.
            "cached_tokens": int(totals_row[2]),
            "cost_usd": round(float(totals_row[3]), 6),
        },
        "by_model": by_model,
    }


def query_cost_report(
    tenant_id: str = "default",
    since_days: int = 30,
) -> list[dict[str, Any]]:
    """Query the AggregatingMergeTree MV for cost breakdown by model and date.

    Queries ``nova.cost_by_model_mv`` using ``sumMerge`` / ``countMerge``
    to combine partial aggregation states across ClickHouse parts.

    Args:
        tenant_id:  Tenant to filter by (default: "default").
        since_days: Number of days to look back (default: 30).

    Returns:
        List of dicts with keys: model_id, date, total_usd, prompt_tokens,
        completion_tokens, run_count.  Ordered by date DESC, total_usd DESC.

    Raises:
        RuntimeError: if NOVA_CLICKHOUSE_URL is not set.
    """
    client = _get_client()

    sql = """
        SELECT
            model_id,
            date,
            sumMerge(total_usd_state)           AS total_usd,
            toUInt64(sumMerge(prompt_tokens_state))      AS prompt_tokens,
            toUInt64(sumMerge(completion_tokens_state))  AS completion_tokens,
            countMerge(run_count_state)         AS run_count
        FROM nova.cost_by_model_mv
        WHERE tenant_id = {tenant_id:String}
          AND date >= today() - toIntervalDay(toInt32({since_days:Int32}))
        GROUP BY model_id, date
        ORDER BY date DESC, total_usd DESC
    """

    result = client.query(
        sql,
        parameters={"tenant_id": tenant_id, "since_days": since_days},
    )

    rows = []
    for row in result.result_rows:
        rows.append(
            {
                "model_id": row[0],
                "date": str(row[1]),
                "total_usd": round(float(row[2]), 6),
                "prompt_tokens": int(row[3]),
                "completion_tokens": int(row[4]),
                "run_count": int(row[5]),
            }
        )
    return rows
