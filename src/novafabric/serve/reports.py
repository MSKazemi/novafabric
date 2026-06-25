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


def _summaries_from_cache(
    db_path: Path,
    from_ts: str | None,
    to_ts: str | None,
    status: str | None = None,
) -> list[dict[str, Any]] | None:
    """Return run summaries from the runs_cache index, or ``None`` to fall back.

    Returns ``None`` (signalling the caller to scan the capsule filesystem) when
    the cache table is empty or unavailable — never an empty list for an empty
    cache, so an unpopulated index does not masquerade as "zero runs".

    The cache stores exactly the columns ``list_run_summaries`` produces, so the
    returned dicts are field-compatible with the disk-scan path.  Date and
    status filters are applied at the SQL layer via ``query_runs``.
    """
    import sqlite3

    from novafabric.registry.runs_cache import (
        count_cached_runs,
        ensure_runs_cache,
        query_runs,
    )

    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            ensure_runs_cache(con)
            if count_cached_runs(con) == 0:
                return None
            rows, _ = query_runs(
                con,
                limit=1_000_000,
                offset=0,
                since=from_ts,
                until=to_ts,
                status=status,
            )
            return rows
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return None

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


# ── 2. Cost Burn ──────────────────────────────────────────────────────────────

COST_BURN_COLS = ["agent", "runs", "model_calls", "tool_calls"]

def report_cost_burn(
    capsule_dir: Path,
    from_ts: str | None = None,
    to_ts: str | None = None,
    db_path: Path | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    cached = (
        _summaries_from_cache(db_path, from_ts, to_ts)
        if db_path is not None
        else None
    )
    if cached is not None:
        rows = cached  # date already filtered in SQL
    else:
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
) -> tuple[list[str], list[dict[str, Any]]]:
    rows = list_run_summaries(capsule_dir)
    rows = _filter_by_date(rows, from_ts, to_ts)
    prefix_len = {"1h": 13, "1d": 10, "1w": 7}.get(resolution, 10)
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
) -> tuple[list[str], list[dict[str, Any]]]:
    rows = list_run_summaries(capsule_dir)
    rows = _filter_by_date(rows, from_ts, to_ts)
    total = len(rows)
    successes = sum(1 for r in rows if r.get("exit_code") == 0 or r.get("status") == "ok")
    failures = total - successes
    model_calls = sum(int(r.get("model_call_count") or 0) for r in rows)
    tool_calls = sum(int(r.get("tool_call_count") or 0) for r in rows)
    period = f"{from_ts or 'all'} – {to_ts or 'now'}"
    rate = round(successes / total * 100, 1) if total else 0.0
    out: list[dict[str, Any]] = [{
        "period": period,
        "total_runs": total,
        "successes": successes,
        "failures": failures,
        "success_rate_pct": rate,
        "total_model_calls": model_calls,
        "total_tool_calls": tool_calls,
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
