"""Unit coverage for the serve report builders' populated and error paths.

The existing tests/serve/test_reports.py exercises the empty / no-db early
returns; this file covers the populated-DB query paths, the per-filter
branches, the score-JSON parsing (valid + malformed), the capsule-compare
manifest paths, and the broken-DB exception fallbacks — the bulk of the
previously-uncovered surface in ``novafabric.serve.reports`` (item #4 tranche).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from novafabric.serve.reports import (
    EVAL_REGRESSION_COLS,
    POLICY_AUDIT_COLS,
    RELEASE_COMPARISON_COLS,
    SEAL_VERIFICATION_COLS,
    report_capsule_compare,
    report_eval_regression,
    report_policy_audit,
    report_release_comparison,
    report_seal_verification,
)


def _db(tmp_path: Path, name: str = "reports.db") -> Path:
    return tmp_path / name


# --------------------------------------------------------------------------- #
# report_eval_regression
# --------------------------------------------------------------------------- #

def _make_eval_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE eval_results "
        "(suite_name TEXT, passed INTEGER, score_json TEXT, run_at TEXT, "
        " novafabric_version TEXT)"
    )
    con.executemany(
        "INSERT INTO eval_results VALUES (?,?,?,?,?)",
        [
            ("gaia", 1, '{"score": 0.9}', "2026-05-01T00:00:00Z", "0.50.0"),
            ("gaia", 0, "not-json", "2026-05-02T00:00:00Z", "0.51.0"),
            ("swe", 1, None, "2026-05-03T00:00:00Z", "0.51.0"),
        ],
    )
    con.commit()
    con.close()


def test_eval_regression_populated_and_score_parsing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _make_eval_db(db)
    cols, rows = report_eval_regression(db)
    assert cols == EVAL_REGRESSION_COLS
    assert len(rows) == 3
    by_suite = {(r["suite_name"], r["run_at"]): r for r in rows}
    # valid JSON score parsed
    assert by_suite[("gaia", "2026-05-01T00:00:00Z")]["score"] == 0.9
    assert by_suite[("gaia", "2026-05-01T00:00:00Z")]["passed"] is True
    # malformed JSON -> score None
    assert by_suite[("gaia", "2026-05-02T00:00:00Z")]["score"] is None
    # null score_json -> score None
    assert by_suite[("swe", "2026-05-03T00:00:00Z")]["score"] is None


def test_eval_regression_filters(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _make_eval_db(db)
    _, rows = report_eval_regression(
        db, from_ts="2026-05-02T00:00:00Z", to_ts="2026-05-03T00:00:00Z", suite="swe"
    )
    assert [r["suite_name"] for r in rows] == ["swe"]


def test_eval_regression_broken_db_returns_empty(tmp_path: Path) -> None:
    db = _db(tmp_path, "empty.db")
    db.write_text("")  # exists but has no eval_results table -> sqlite error path
    cols, rows = report_eval_regression(db)
    assert cols == EVAL_REGRESSION_COLS
    assert rows == []


# --------------------------------------------------------------------------- #
# report_policy_audit
# --------------------------------------------------------------------------- #

def test_policy_audit_populated_and_filters(tmp_path: Path) -> None:
    db = _db(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE policy_checks "
        "(policy_id TEXT, run_id TEXT, result TEXT, message TEXT, checked_at TEXT)"
    )
    con.executemany(
        "INSERT INTO policy_checks VALUES (?,?,?,?,?)",
        [
            ("p1", "r1", "allow", "ok", "2026-05-01T00:00:00Z"),
            ("p1", "r2", "deny", "blocked", "2026-05-02T00:00:00Z"),
            ("p2", "r3", "allow", "ok", "2026-05-03T00:00:00Z"),
        ],
    )
    con.commit()
    con.close()

    cols, rows = report_policy_audit(db)
    assert cols == POLICY_AUDIT_COLS
    assert len(rows) == 3
    _, denied = report_policy_audit(
        db, policy_id="p1", result="deny",
        from_ts="2026-05-01T00:00:00Z", to_ts="2026-05-09T00:00:00Z",
    )
    assert [r["run_id"] for r in denied] == ["r2"]


def test_policy_audit_broken_db_returns_empty(tmp_path: Path) -> None:
    db = _db(tmp_path, "bad.db")
    db.write_text("")
    cols, rows = report_policy_audit(db)
    assert cols == POLICY_AUDIT_COLS and rows == []


# --------------------------------------------------------------------------- #
# report_seal_verification
# --------------------------------------------------------------------------- #

def test_seal_verification_populated_and_filters(tmp_path: Path) -> None:
    db = _db(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE seal_proposals "
        "(capsule_id TEXT, proposer TEXT, status TEXT, proposed_at TEXT)"
    )
    con.executemany(
        "INSERT INTO seal_proposals VALUES (?,?,?,?)",
        [
            ("c1", "alice", "approved", "2026-05-01T00:00:00Z"),
            ("c2", "bob", "pending", "2026-05-05T00:00:00Z"),
        ],
    )
    con.commit()
    con.close()

    cols, rows = report_seal_verification(db)
    assert cols == SEAL_VERIFICATION_COLS
    assert len(rows) == 2
    _, windowed = report_seal_verification(
        db, from_ts="2026-05-03T00:00:00Z", to_ts="2026-05-09T00:00:00Z"
    )
    assert [r["capsule_id"] for r in windowed] == ["c2"]


def test_seal_verification_broken_db_returns_empty(tmp_path: Path) -> None:
    db = _db(tmp_path, "bad.db")
    db.write_text("")
    cols, rows = report_seal_verification(db)
    assert cols == SEAL_VERIFICATION_COLS and rows == []


# --------------------------------------------------------------------------- #
# report_capsule_compare
# --------------------------------------------------------------------------- #

def _write_capsule(base: Path, run_id: str, **fields: object) -> None:
    d = base / run_id
    d.mkdir(parents=True)
    lines = [f"run_id: {run_id}"] + [f"{k}: {v}" for k, v in fields.items()]
    (d / "capsule.yaml").write_text("\n".join(lines) + "\n")


def test_capsule_compare_changed_and_unchanged_fields(tmp_path: Path) -> None:
    caps = tmp_path / "capsules"
    _write_capsule(caps, "runA", status="ok", exit_code=0, model_call_count=3)
    _write_capsule(caps, "runB", status="ok", exit_code=1, model_call_count=3)
    cols, rows = report_capsule_compare(caps, "runA", "runB")
    assert cols == ["field", "value_a", "value_b", "changed"]
    by_field = {r["field"]: r for r in rows}
    # exit_code differs -> changed True; status/model_call_count equal -> changed False
    assert by_field["exit_code"]["changed"] is True
    assert by_field["status"]["changed"] is False
    assert by_field["model_call_count"]["changed"] is False


def test_capsule_compare_missing_runs_yield_none(tmp_path: Path) -> None:
    caps = tmp_path / "capsules"
    caps.mkdir()
    _, rows = report_capsule_compare(caps, "nope1", "nope2")
    # both manifests missing -> all values None, changed False
    assert all(r["value_a"] is None and r["value_b"] is None for r in rows)
    assert all(r["changed"] is False for r in rows)


def test_capsule_compare_malformed_manifest_is_treated_as_missing(tmp_path: Path) -> None:
    caps = tmp_path / "capsules"
    d = caps / "runBad"
    d.mkdir(parents=True)
    # Invalid YAML -> load_capsule_manifest raises -> _find swallows -> None.
    (d / "capsule.yaml").write_text("run_id: runBad\n  : : bad: [unclosed\n")
    _, rows = report_capsule_compare(caps, "runBad", "runBad")
    assert all(r["value_a"] is None and r["value_b"] is None for r in rows)


# --------------------------------------------------------------------------- #
# report_release_comparison
# --------------------------------------------------------------------------- #

def test_release_comparison_delta_and_regression(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _make_eval_db(db)  # gaia@0.50.0 score 0.9 ; gaia@0.51.0 malformed -> None ; swe@0.51.0 None
    # Add a clean regression pair for gaia between two versions.
    con = sqlite3.connect(str(db))
    con.executemany(
        "INSERT INTO eval_results VALUES (?,?,?,?,?)",
        [
            ("gaia", 1, '{"score": 0.95}', "2026-05-10T00:00:00Z", "vA"),
            ("gaia", 1, '{"score": 0.80}', "2026-05-11T00:00:00Z", "vB"),
        ],
    )
    con.commit()
    con.close()

    cols, rows = report_release_comparison(db, "vA", "vB")
    assert cols == RELEASE_COMPARISON_COLS
    gaia = next(r for r in rows if r["suite_name"] == "gaia")
    assert gaia["score_a"] == 0.95
    assert gaia["score_b"] == 0.80
    assert gaia["delta"] == -0.15
    assert gaia["regression"] is True


def test_release_comparison_missing_scores_no_delta(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _make_eval_db(db)
    # 0.50.0 has gaia score; a version with no rows -> score None -> delta None
    _, rows = report_release_comparison(db, "0.50.0", "does-not-exist")
    gaia = next(r for r in rows if r["suite_name"] == "gaia")
    assert gaia["score_a"] == 0.9
    assert gaia["score_b"] is None
    assert gaia["delta"] is None
    assert gaia["regression"] is False


def test_release_comparison_malformed_score_json(tmp_path: Path) -> None:
    db = _db(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE eval_results "
        "(suite_name TEXT, passed INTEGER, score_json TEXT, run_at TEXT, "
        " novafabric_version TEXT)"
    )
    con.executemany(
        "INSERT INTO eval_results VALUES (?,?,?,?,?)",
        [
            ("gaia", 1, "not-json", "2026-05-10T00:00:00Z", "vA"),  # parse error -> None
            ("gaia", 1, '{"score": 0.7}', "2026-05-11T00:00:00Z", "vB"),
        ],
    )
    con.commit()
    con.close()
    _, rows = report_release_comparison(db, "vA", "vB")
    gaia = next(r for r in rows if r["suite_name"] == "gaia")
    assert gaia["score_a"] is None  # malformed JSON swallowed -> None
    assert gaia["score_b"] == 0.7


def test_release_comparison_broken_db_returns_empty(tmp_path: Path) -> None:
    db = _db(tmp_path, "bad.db")
    db.write_text("")
    cols, rows = report_release_comparison(db, "a", "b")
    assert cols == RELEASE_COMPARISON_COLS and rows == []
