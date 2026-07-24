"""Report query functions for /api/reports/* endpoints.

Each function returns (columns: list[str], rows: list[dict]).
Rows are plain dicts with str/int/float/bool/None values — safe for CSV and JSON.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from novafabric.serve.capsule_loader import list_run_summaries

# ── helpers ──────────────────────────────────────────────────────────────────


# Row-level report responses are bounded (ADR-0199 rule 1): aggregate reports
# push their reduction into SQL; the one row-level report (run history) caps at
# this many rows per response until the cursor-paged route envelope lands.
MAX_REPORT_ROWS = 10_000


def _cache_conn(db_path: Path) -> Any | None:
    """Open the runs_cache index, or ``None`` to signal filesystem fallback.

    Returns ``None`` when the cache table is empty or unavailable — never a
    live connection over an empty cache, so an unpopulated index does not
    masquerade as "zero runs". Caller must close the returned connection.
    """
    import sqlite3

    from novafabric.registry.runs_cache import count_cached_runs, ensure_runs_cache

    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        ensure_runs_cache(con)
        if count_cached_runs(con) == 0:
            con.close()
            return None
        return con
    except Exception:  # noqa: BLE001
        return None


def _summaries_from_cache(
    db_path: Path,
    from_ts: str | None,
    to_ts: str | None,
    status: str | None = None,
    limit: int = MAX_REPORT_ROWS,
) -> list[dict[str, Any]] | None:
    """Return run summaries from the runs_cache index, or ``None`` to fall back.

    The cache stores exactly the columns ``list_run_summaries`` produces, so the
    returned dicts are field-compatible with the disk-scan path.  Date and
    status filters are applied at the SQL layer via ``query_runs``.  Bounded to
    ``limit`` newest rows (ADR-0199) — aggregate reports must not use this
    helper; they call the SQL aggregate functions in ``runs_cache`` instead.
    """
    from novafabric.registry.runs_cache import query_runs

    con = _cache_conn(db_path)
    if con is None:
        return None
    try:
        rows, _ = query_runs(
            con,
            limit=limit,
            offset=0,
            since=from_ts,
            until=to_ts,
            status=status,
        )
        return rows
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()

def _ts_str(value: Any) -> str:
    """Normalise a timestamp value (str or datetime) to an ISO 8601 string."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _filter_by_date(
    rows: list[dict[str, Any]],
    from_ts: str | None,
    to_ts: str | None,
    ts_field: str = "created_at",
) -> list[dict[str, Any]]:
    if from_ts:
        rows = [r for r in rows if _ts_str(r.get(ts_field)) >= from_ts]
    if to_ts:
        rows = [r for r in rows if _ts_str(r.get(ts_field)) <= to_ts]
    return rows


def rows_to_csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


# ── 1. Run History ────────────────────────────────────────────────────────────

RUN_HISTORY_COLS = [
    "run_id", "created_at", "finished_at", "status",
    "duration_ms", "exit_code", "model_call_count", "tool_call_count",
]

def report_run_history(
    capsule_dir: Path,
    from_ts: str | None = None,
    to_ts: str | None = None,
    status: str | None = None,
    agent: str | None = None,
    db_path: Path | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    cached = (
        _summaries_from_cache(db_path, from_ts, to_ts, status)
        if db_path is not None
        else None
    )
    if cached is not None:
        rows = cached  # date + status already filtered in SQL
    else:
        rows = list_run_summaries(capsule_dir)
        rows = _filter_by_date(rows, from_ts, to_ts)
        if status and status != "all":
            rows = [r for r in rows if r.get("status") == status]
    if agent:
        rows = [
            r for r in rows
            if agent.lower() in " ".join(r.get("command") or []).lower()
        ]
    out = []
    for r in rows:
        row: dict[str, Any] = {}
        for c in RUN_HISTORY_COLS:
            v = r.get(c)
            row[c] = _ts_str(v) if c in ("created_at", "finished_at") else v
        out.append(row)
    return RUN_HISTORY_COLS, out


def _run_history_project(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        row: dict[str, Any] = {}
        for c in RUN_HISTORY_COLS:
            v = r.get(c)
            row[c] = _ts_str(v) if c in ("created_at", "finished_at") else v
        out.append(row)
    return out


def report_run_history_page(
    capsule_dir: Path,
    from_ts: str | None = None,
    to_ts: str | None = None,
    status: str | None = None,
    agent: str | None = None,
    db_path: Path | None = None,
    limit: int = 1000,
    after: tuple[str, str] | None = None,
) -> tuple[list[str], list[dict[str, Any]], tuple[str, str] | None, int | None]:
    """Keyset-paged run history (ADR-0199 rule 2).

    Returns ``(columns, rows, next_after, total)``. ``next_after`` is the
    ``(created_at, run_id)`` position of the last returned row when a further
    page may exist, else ``None``. Falls back to the (bounded) filesystem path
    with ``total=None`` when the cache is unavailable — offset-free, one page.
    """
    con = _cache_conn(db_path) if db_path is not None else None
    if con is None:
        cols, rows = report_run_history(
            capsule_dir, from_ts, to_ts, status, agent, db_path=None
        )
        return cols, rows[:limit], None, None
    try:
        from novafabric.registry.runs_cache import query_runs

        raw, total = query_runs(
            con,
            limit=limit,
            since=from_ts,
            until=to_ts,
            status=status,
            agent=agent,
            after=after,
        )
    finally:
        con.close()
    rows = _run_history_project(raw)
    next_after: tuple[str, str] | None = None
    if len(raw) == limit and raw:
        last = raw[-1]
        next_after = (str(last.get("created_at") or ""), str(last.get("run_id") or ""))
    return RUN_HISTORY_COLS, rows, next_after, total


def iter_run_history_csv(
    capsule_dir: Path,
    from_ts: str | None = None,
    to_ts: str | None = None,
    status: str | None = None,
    agent: str | None = None,
    db_path: Path | None = None,
    page_size: int = 1000,
) -> Any:
    """Yield the run-history CSV in keyset pages — bounded memory at any size."""
    header_written = False
    after: tuple[str, str] | None = None
    while True:
        cols, rows, after, _total = report_run_history_page(
            capsule_dir,
            from_ts,
            to_ts,
            status,
            agent,
            db_path=db_path,
            limit=page_size,
            after=after,
        )
        chunk = rows_to_csv(cols, rows)
        if header_written:
            # rows_to_csv repeats the header; strip it on later pages.
            chunk = chunk.split("\r\n", 1)[1] if "\r\n" in chunk else ""
        if chunk:
            yield chunk
        header_written = True
        if after is None:
            return


# ── 2. Cost Burn ──────────────────────────────────────────────────────────────

COST_BURN_COLS = ["agent", "runs", "model_calls", "tool_calls"]

def report_cost_burn(
    capsule_dir: Path,
    from_ts: str | None = None,
    to_ts: str | None = None,
    db_path: Path | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if db_path is not None:
        con = _cache_conn(db_path)
        if con is not None:
            from novafabric.registry.runs_cache import aggregate_runs_by_agent

            try:
                out = aggregate_runs_by_agent(con, since=from_ts, until=to_ts)
                return COST_BURN_COLS, out
            except Exception:  # noqa: BLE001
                pass  # fall through to the filesystem scan
            finally:
                con.close()
    rows = list_run_summaries(capsule_dir)
    rows = _filter_by_date(rows, from_ts, to_ts)
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        cmd = r.get("command") or []
        key = cmd[0] if cmd else "(unknown)"
        b = buckets.setdefault(key, {"agent": key, "runs": 0, "model_calls": 0, "tool_calls": 0})
        b["runs"] += 1
        b["model_calls"] += int(r.get("model_call_count") or 0)
        b["tool_calls"] += int(r.get("tool_call_count") or 0)
    out = sorted(buckets.values(), key=lambda x: x["runs"], reverse=True)
    return COST_BURN_COLS, out


# ── 3. Throughput ─────────────────────────────────────────────────────────────

THROUGHPUT_COLS = ["window", "runs", "successes", "failures", "success_rate_pct"]

def report_throughput(
    capsule_dir: Path,
    from_ts: str | None = None,
    to_ts: str | None = None,
    resolution: str = "1d",
    db_path: Path | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    prefix_len = {"1h": 13, "1d": 10, "1w": 7}.get(resolution, 10)
    if db_path is not None:
        con = _cache_conn(db_path)
        if con is not None:
            from novafabric.registry.runs_cache import aggregate_runs_windowed

            try:
                windows = aggregate_runs_windowed(
                    con, prefix_len=prefix_len, since=from_ts, until=to_ts
                )
                out = []
                for b in windows:
                    rate = (
                        round(b["successes"] / b["runs"] * 100, 1) if b["runs"] else 0.0
                    )
                    out.append({**b, "success_rate_pct": rate})
                return THROUGHPUT_COLS, out
            except Exception:  # noqa: BLE001
                pass  # fall through to the filesystem scan
            finally:
                con.close()
    rows = list_run_summaries(capsule_dir)
    rows = _filter_by_date(rows, from_ts, to_ts)
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        ts = _ts_str(r.get("created_at"))[:prefix_len]
        if not ts:
            continue
        b = buckets.setdefault(ts, {"window": ts, "runs": 0, "successes": 0, "failures": 0})
        b["runs"] += 1
        if r.get("exit_code") == 0 or r.get("status") == "ok":
            b["successes"] += 1
        else:
            b["failures"] += 1
    out = []
    for b in sorted(buckets.values(), key=lambda x: x["window"]):
        rate = round(b["successes"] / b["runs"] * 100, 1) if b["runs"] else 0.0
        out.append({**b, "success_rate_pct": rate})
    return THROUGHPUT_COLS, out


# ── 4. Executive Summary ──────────────────────────────────────────────────────

EXECUTIVE_COLS = [
    "period", "total_runs", "successes", "failures",
    "success_rate_pct", "total_model_calls", "total_tool_calls",
]

def report_executive_summary(
    capsule_dir: Path,
    from_ts: str | None = None,
    to_ts: str | None = None,
    db_path: Path | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    totals: dict[str, int] | None = None
    if db_path is not None:
        con = _cache_conn(db_path)
        if con is not None:
            from novafabric.registry.runs_cache import runs_totals

            try:
                totals = runs_totals(con, since=from_ts, until=to_ts)
            except Exception:  # noqa: BLE001
                totals = None
            finally:
                con.close()
    if totals is None:
        rows = list_run_summaries(capsule_dir)
        rows = _filter_by_date(rows, from_ts, to_ts)
        total = len(rows)
        successes = sum(
            1 for r in rows if r.get("exit_code") == 0 or r.get("status") == "ok"
        )
        totals = {
            "total_runs": total,
            "successes": successes,
            "failures": total - successes,
            "model_calls": sum(int(r.get("model_call_count") or 0) for r in rows),
            "tool_calls": sum(int(r.get("tool_call_count") or 0) for r in rows),
        }
    period = f"{from_ts or 'all'} – {to_ts or 'now'}"
    rate = (
        round(totals["successes"] / totals["total_runs"] * 100, 1)
        if totals["total_runs"]
        else 0.0
    )
    out: list[dict[str, Any]] = [{
        "period": period,
        "total_runs": totals["total_runs"],
        "successes": totals["successes"],
        "failures": totals["failures"],
        "success_rate_pct": rate,
        "total_model_calls": totals["model_calls"],
        "total_tool_calls": totals["tool_calls"],
    }]
    return EXECUTIVE_COLS, out


# ── 5. Evidence Inventory ─────────────────────────────────────────────────────

EVIDENCE_COLS = ["bundle_id", "run_id", "created_at", "size_bytes", "verified"]

def report_evidence_inventory(
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    import hashlib
    import json as _json
    import os
    import zipfile

    override = os.environ.get("NOVAFABRIC_EVIDENCE_DIR")
    evidence_dir = Path(override) if override else Path.home() / ".novafabric" / "evidence"
    if not evidence_dir.is_dir():
        return EVIDENCE_COLS, []

    out: list[dict[str, Any]] = []
    for zp in sorted(evidence_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        bundle_id = zp.stem
        size_bytes = zp.stat().st_size
        run_id = bundle_id
        created_at: str | None = None
        verified = False
        try:
            with zipfile.ZipFile(zp, "r") as zf:
                if "manifest.json" in zf.namelist():
                    m = _json.loads(zf.read("manifest.json"))
                    run_id = m.get("run_id", bundle_id)
                    created_at = m.get("created_at") or m.get("timestamp")
                    stored = m.get("manifest_hash", "")
                    work = {k: v for k, v in m.items() if k != "manifest_hash"}
                    recomputed = hashlib.sha256(
                        _json.dumps(work, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    verified = stored == recomputed
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "bundle_id": bundle_id,
            "run_id": run_id,
            "created_at": created_at,
            "size_bytes": size_bytes,
            "verified": verified,
        })

    out = _filter_by_date(out, from_ts, to_ts)
    return EVIDENCE_COLS, out


# ── 6–10: DB-backed reports (degrade gracefully when table absent) ─────────────

EVAL_REGRESSION_COLS = ["suite_name", "run_at", "passed", "score"]

def report_eval_regression(
    db_path: Path | None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    suite: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if not db_path or not db_path.exists():
        return EVAL_REGRESSION_COLS, []
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        sql = "SELECT suite_name, passed, score_json, run_at FROM eval_results WHERE 1=1"
        params: list[Any] = []
        if from_ts:
            sql += " AND run_at >= ?"
            params.append(from_ts)
        if to_ts:
            sql += " AND run_at <= ?"
            params.append(to_ts)
        if suite:
            sql += " AND suite_name = ?"
            params.append(suite)
        sql += " ORDER BY run_at DESC LIMIT 500"
        rows = con.execute(sql, params).fetchall()
        con.close()
        import json as _json
        out = []
        for r in rows:
            score_raw = r["score_json"]
            try:
                score = _json.loads(score_raw).get("score") if score_raw else None
            except Exception:  # noqa: BLE001
                score = None
            out.append({
                "suite_name": r["suite_name"],
                "run_at": r["run_at"],
                "passed": bool(r["passed"]),
                "score": score,
            })
        return EVAL_REGRESSION_COLS, out
    except Exception:  # noqa: BLE001
        return EVAL_REGRESSION_COLS, []


POLICY_AUDIT_COLS = ["policy_id", "run_id", "result", "message", "checked_at"]

def report_policy_audit(
    db_path: Path | None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    policy_id: str | None = None,
    result: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if not db_path or not db_path.exists():
        return POLICY_AUDIT_COLS, []
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        sql = "SELECT policy_id, run_id, result, message, checked_at FROM policy_checks WHERE 1=1"
        params: list[Any] = []
        if from_ts:
            sql += " AND checked_at >= ?"
            params.append(from_ts)
        if to_ts:
            sql += " AND checked_at <= ?"
            params.append(to_ts)
        if policy_id:
            sql += " AND policy_id = ?"
            params.append(policy_id)
        if result:
            sql += " AND result = ?"
            params.append(result)
        sql += " ORDER BY checked_at DESC LIMIT 500"
        rows = con.execute(sql, params).fetchall()
        con.close()
        return POLICY_AUDIT_COLS, [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return POLICY_AUDIT_COLS, []


SEAL_VERIFICATION_COLS = ["capsule_id", "proposer", "status", "proposed_at"]

def report_seal_verification(
    db_path: Path | None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if not db_path or not db_path.exists():
        return SEAL_VERIFICATION_COLS, []
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        sql = "SELECT capsule_id, proposer, status, proposed_at FROM seal_proposals WHERE 1=1"
        params: list[Any] = []
        if from_ts:
            sql += " AND proposed_at >= ?"
            params.append(from_ts)
        if to_ts:
            sql += " AND proposed_at <= ?"
            params.append(to_ts)
        sql += " ORDER BY proposed_at DESC LIMIT 500"
        rows = con.execute(sql, params).fetchall()
        con.close()
        return SEAL_VERIFICATION_COLS, [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return SEAL_VERIFICATION_COLS, []


CAPSULE_COMPARE_COLS = ["field", "value_a", "value_b", "changed"]
CAPSULE_COMPARE_FIELDS = [
    "status", "duration_ms", "exit_code", "model_call_count",
    "tool_call_count", "novafabric_version",
]

def report_capsule_compare(
    capsule_dir: Path,
    run_a: str,
    run_b: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    from novafabric.serve.capsule_loader import discover_capsule_dirs, load_capsule_manifest

    def _find(run_id: str) -> dict[str, Any] | None:
        for d in discover_capsule_dirs(capsule_dir):
            if d.name == run_id or d.name.startswith(run_id):
                try:
                    return load_capsule_manifest(d)
                except Exception:  # noqa: BLE001
                    return None
        return None

    ma = _find(run_a) or {}
    mb = _find(run_b) or {}
    out = []
    for field in CAPSULE_COMPARE_FIELDS:
        va, vb = ma.get(field), mb.get(field)
        out.append({"field": field, "value_a": va, "value_b": vb, "changed": va != vb})
    return CAPSULE_COMPARE_COLS, out


RELEASE_COMPARISON_COLS = ["suite_name", "score_a", "score_b", "delta", "regression"]

def report_release_comparison(
    db_path: Path | None,
    version_a: str,
    version_b: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Compare latest eval scores for two novafabric_version values."""
    if not db_path or not db_path.exists():
        return RELEASE_COMPARISON_COLS, []
    import json as _json
    import sqlite3
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row

        def _latest_scores(version: str) -> dict[str, float | None]:
            rows = con.execute(
                "SELECT suite_name, score_json FROM eval_results "
                "WHERE novafabric_version = ? ORDER BY run_at DESC LIMIT 200",
                (version,),
            ).fetchall()
            seen: dict[str, float | None] = {}
            for r in rows:
                sn = r["suite_name"]
                if sn not in seen:
                    try:
                        seen[sn] = _json.loads(r["score_json"]).get("score")
                    except Exception:  # noqa: BLE001
                        seen[sn] = None
            return seen

        scores_a = _latest_scores(version_a)
        scores_b = _latest_scores(version_b)
        con.close()
        suites = sorted(set(scores_a) | set(scores_b))
        out = []
        for s in suites:
            sa, sb = scores_a.get(s), scores_b.get(s)
            delta = round(sb - sa, 4) if sa is not None and sb is not None else None
            regression = bool(delta is not None and delta < -0.01)
            out.append({
                "suite_name": s,
                "score_a": sa,
                "score_b": sb,
                "delta": delta,
                "regression": regression,
            })
        return RELEASE_COMPARISON_COLS, out
    except Exception:  # noqa: BLE001
        return RELEASE_COMPARISON_COLS, []


# ── 11. Alert Digest (R4) ─────────────────────────────────────────────────────

ALERT_DIGEST_COLS = [
    "ts", "kind", "event_type", "severity_or_outcome", "endpoint_or_source", "detail",
]

#: Bounded scan window per source (ADR-0199 rule 1) — same tail-read helpers
#: the alerts router uses, so IO stays O(limit), never O(file).
ALERT_DIGEST_LIMIT = 1000


def report_alert_digest(
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = ALERT_DIGEST_LIMIT,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Alert delivery outcomes + emitted ops.* events, newest first.

    Reads the same two sources as ``GET /api/alerts/recent`` via the shared
    row parsers in :mod:`novafabric.serve.routers.alerts`. ``kind`` is
    ``delivery`` (hash-chained audit log; ``severity_or_outcome`` carries the
    delivery outcome ``delivered``/``failed``) or ``emitted`` (durable events
    log; ``severity_or_outcome`` carries the event severity). Missing files or
    unreadable config degrade to empty rows — never an exception.
    """
    try:
        from novafabric.audit import AUDIT_LOG_PATH
        from novafabric.events.alerts import load_alerts_config_from_env
        from novafabric.events.emitter import load_config_from_env
        from novafabric.serve.routers.alerts import (
            delivery_rows,
            emitted_rows,
            parse_alert_ts,
        )

        alerts_cfg = load_alerts_config_from_env()
        events_cfg = load_config_from_env()
        audit_path = alerts_cfg.audit_log_path or AUDIT_LOG_PATH
    except Exception:  # noqa: BLE001
        return ALERT_DIGEST_COLS, []

    merged: list[dict[str, Any]] = []
    covered: set[str] = set()
    try:
        merged, covered = delivery_rows(audit_path, limit)
    except Exception:  # noqa: BLE001
        merged, covered = [], set()
    if events_cfg.log_path is not None:
        try:
            merged.extend(emitted_rows(events_cfg.log_path, covered, limit))
        except Exception:  # noqa: BLE001
            pass
    merged.sort(key=lambda r: parse_alert_ts(str(r.get("timestamp", ""))), reverse=True)

    out: list[dict[str, Any]] = []
    for r in merged:
        is_delivery = r.get("outcome") != "emitted"
        if is_delivery:
            severity_or_outcome = r.get("outcome")
            endpoint_or_source = r.get("endpoint_id")
            detail = (
                f"severity={r.get('severity')} attempts={r.get('attempts')} "
                f"subject={r.get('subject')}"
            )
        else:
            severity_or_outcome = r.get("severity")
            endpoint_or_source = r.get("subject")
            detail = f"event_id={r.get('id')}"
        out.append({
            "ts": r.get("timestamp"),
            "kind": "delivery" if is_delivery else "emitted",
            "event_type": r.get("event_type"),
            "severity_or_outcome": severity_or_outcome,
            "endpoint_or_source": endpoint_or_source,
            "detail": detail,
        })
    out = _filter_by_date(out, from_ts, to_ts, ts_field="ts")
    return ALERT_DIGEST_COLS, out[:limit]


# ── 12. API Key Inventory (R4) ────────────────────────────────────────────────

API_KEY_INVENTORY_COLS = [
    "key_id", "name_or_owner", "roles", "created_at",
    "expires_at", "last_used_at", "status",
]


def report_api_key_inventory(
    db_path: Path | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Secret-free API-key inventory via the admin-keys projection helper.

    ``status`` is derived (active/revoked/expired) — it is not a stored
    column. Never hashes, never secrets; missing store → empty rows.
    """
    from novafabric.serve.routers.admin_keys import api_key_rows

    try:
        rows = api_key_rows(db_path)
    except Exception:  # noqa: BLE001
        return API_KEY_INVENTORY_COLS, []
    out: list[dict[str, Any]] = []
    for r in rows:
        roles = r.get("roles")
        out.append({
            "key_id": r.get("key_id"),
            "name_or_owner": r.get("owner"),
            "roles": ",".join(roles) if isinstance(roles, list) else roles,
            "created_at": r.get("created_at"),
            "expires_at": r.get("expires_at"),
            "last_used_at": r.get("last_used_at"),
            "status": r.get("status"),
        })
    return API_KEY_INVENTORY_COLS, out


# ── 13. Dashboard Audit (R4) ──────────────────────────────────────────────────

DASHBOARD_AUDIT_COLS = ["ts", "action", "cli_equivalent", "actor_token_fp", "result"]

#: Hard cap for the report (bounded tail read, ADR-0199 rule 1).
DASHBOARD_AUDIT_LIMIT = 5000


def report_dashboard_audit(
    action: str | None = None,
    limit: int = DASHBOARD_AUDIT_LIMIT,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Recent dashboard-audit entries (Layer B mutations), newest first.

    Reads the append-only ``dashboard-audit.jsonl`` via the bounded
    :func:`novafabric.serve.audit.read_recent_tail` helper. Optional exact
    ``action`` filter. Missing log → empty rows.
    """
    from novafabric.serve.audit import read_recent_tail

    try:
        entries, _cursor = read_recent_tail(
            limit=min(limit, DASHBOARD_AUDIT_LIMIT), action=action or None
        )
    except Exception:  # noqa: BLE001
        return DASHBOARD_AUDIT_COLS, []
    out = [
        {c: e.get(c) for c in DASHBOARD_AUDIT_COLS}
        for e in entries
    ]
    return DASHBOARD_AUDIT_COLS, out


# ── 14. Compliance Posture — Annex IV completeness (R4) ───────────────────────

COMPLIANCE_POSTURE_COLS = [
    "element_id", "title", "population_method", "completeness_flag",
]


def report_compliance_posture(
    capsule_dir: Path,
    run_id: str,
    deployment_id: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Per-element EU AI Act Annex IV completeness for one run capsule.

    The Annex IV builder needs a capsule directory (``capsule.yaml``), so
    ``run_id`` is a required filter. ``deployment_id`` is a label echoed into
    the document; it defaults to the run id. Unknown run, invalid capsule, or
    an unavailable compliance module all degrade to empty rows — never a 500.
    """
    if not run_id or "/" in run_id or ".." in run_id:
        return COMPLIANCE_POSTURE_COLS, []
    try:
        from novafabric.compliance.export.annex_iv import AnnexIVExporter
        from novafabric.serve.capsule_loader import (
            discover_capsule_dirs,
            load_capsule_manifest,
        )
    except Exception:  # noqa: BLE001 — optional compliance module absent
        return COMPLIANCE_POSTURE_COLS, []

    def _find(rid: str) -> Path | None:
        candidate = capsule_dir / rid
        if candidate.is_dir() and (candidate / "capsule.yaml").exists():
            return candidate
        for d in discover_capsule_dirs(capsule_dir):
            try:
                m = load_capsule_manifest(d)
            except Exception:  # noqa: BLE001
                continue
            if m.get("run_id") == rid:
                return d
        return None

    capsule_path = _find(run_id)
    if capsule_path is None:
        return COMPLIANCE_POSTURE_COLS, []
    try:
        document = AnnexIVExporter().build_annex_iv_document(
            deployment_id=deployment_id or run_id,
            capsule_dir=capsule_path,
        )
    except Exception:  # noqa: BLE001
        return COMPLIANCE_POSTURE_COLS, []
    out = [
        {
            "element_id": e.element_id,
            "title": e.element_title,
            "population_method": e.population_method,
            "completeness_flag": e.completeness_flag,
        }
        for e in document.elements
    ]
    return COMPLIANCE_POSTURE_COLS, out
