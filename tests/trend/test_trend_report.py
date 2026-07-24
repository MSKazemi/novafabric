"""Trend-report builder tests (ADR-0131) — bucketing, aggregation, skips.

Guards the behavioral invariants of ``design/spec/trend-report-v0.md``:
explicit gap buckets, skipped-not-aborted capsules, per-metric aggregation,
and the closed ``TrendReport`` JSON shape (validated against the graduated
``schemas/trend-report.schema.json``).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from novafabric.trend import TrendError, TrendUsageError, build_trend_report
from novafabric.views.model import SavedView
from novafabric.views.store import save_view
from trend.conftest import CapsuleFactory, RecordFactory

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "trend-report.schema.json"
_VALIDATOR = Draft202012Validator(
    json.loads(_SCHEMA_PATH.read_text()), format_checker=FormatChecker()
)

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
SINCE = "2026-07-09T00:00:00Z"
UNTIL = "2026-07-13T00:00:00Z"


def _build(capsule_dir: Path, **kwargs: object) -> dict:
    kwargs.setdefault("now", NOW)
    report = build_trend_report(capsule_dir, **kwargs)  # type: ignore[arg-type]
    _VALIDATOR.validate(report)  # every emitted report is schema-valid
    return report


# ── time bucketing ────────────────────────────────────────────────────────────


def test_day_buckets_with_explicit_gaps(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z",
                 model_calls=[model_call(cost=0.02)])
    make_capsule("run-b", created_at="2026-07-10T22:00:00Z",
                 model_calls=[model_call(cost=0.03)])
    make_capsule("run-c", created_at="2026-07-12T01:00:00Z",
                 model_calls=[model_call(cost=0.05)])
    report = _build(capsule_dir, metric="cost", since=SINCE, until=UNTIL)

    assert [p["bucket"] for p in report["series"]] == [
        "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"
    ]
    gap_9, day_10, gap_11, day_12 = report["series"]
    assert gap_9 == {"bucket": "2026-07-09", "value": None, "n": 0,
                     "bucket_start": "2026-07-09T00:00:00Z"}
    assert day_10["value"] == pytest.approx(0.05)
    assert day_10["n"] == 2
    assert gap_11["value"] is None and gap_11["n"] == 0
    assert day_12["value"] == pytest.approx(0.05) and day_12["n"] == 1
    assert report["capsule_count"] == 3
    assert report["skipped_count"] == 0
    assert report["window"] == {"since": SINCE, "until": UNTIL}
    assert report["unit"] == {"kind": "currency", "currency": "USD"}
    assert any("2 gap bucket(s)" in w for w in report["warnings"])


def test_week_buckets_utc_iso_calendar(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    # 2026-07-08 is ISO week 2026-W28 (Monday 2026-07-06); 2026-07-13 starts W29.
    make_capsule("run-a", created_at="2026-07-08T00:00:00Z",
                 model_calls=[model_call(cost=0.10)])
    make_capsule("run-b", created_at="2026-07-13T00:00:00Z",
                 model_calls=[model_call(cost=0.20)])
    report = _build(
        capsule_dir, metric="cost", group_by="week",
        since="2026-06-29T00:00:00Z", until="2026-07-14T00:00:00Z",
    )
    assert [(p["bucket"], p["bucket_start"]) for p in report["series"]] == [
        ("2026-W27", "2026-06-29T00:00:00Z"),
        ("2026-W28", "2026-07-06T00:00:00Z"),
        ("2026-W29", "2026-07-13T00:00:00Z"),
    ]
    w27, w28, w29 = report["series"]
    assert w27["value"] is None and w27["n"] == 0  # gap week, never dropped
    assert w28["value"] == pytest.approx(0.10)
    assert w29["value"] == pytest.approx(0.20)


def test_pathological_window_is_refused(
    make_capsule: CapsuleFactory, capsule_dir: Path
) -> None:
    with pytest.raises(TrendUsageError, match="more than 5000 day buckets"):
        build_trend_report(capsule_dir, metric="cost", since="16000d", now=NOW)


# ── metric aggregation ────────────────────────────────────────────────────────


def test_score_metric_averages_named_suite(
    make_capsule: CapsuleFactory, capsule_dir: Path, score: RecordFactory
) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z",
                 scores=[score("gaia", 0.6), score("swe-bench", 0.1)])
    make_capsule("run-b", created_at="2026-07-10T09:00:00Z",
                 scores=[score("gaia", 0.8)])
    report = _build(capsule_dir, metric="score:gaia", since=SINCE, until=UNTIL)
    day_10 = report["series"][1]
    assert day_10["bucket"] == "2026-07-10"
    assert day_10["value"] == pytest.approx(0.7)  # avg, not sum
    assert day_10["n"] == 2
    assert report["unit"] == {"kind": "score", "name": "gaia"}


def test_unknown_score_name_is_all_gaps_with_warning(
    make_capsule: CapsuleFactory, capsule_dir: Path, score: RecordFactory
) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z", scores=[score("gaia", 0.6)])
    report = _build(capsule_dir, metric="score:nope", since=SINCE, until=UNTIL)
    assert all(p["value"] is None for p in report["series"])
    assert report["capsule_count"] == 0
    assert report["skipped_count"] == 1
    assert any("no score 'nope' recorded" in w for w in report["warnings"])


def test_latency_default_stat_is_p95(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule(
        "run-a", created_at="2026-07-10T08:00:00Z",
        model_calls=[model_call(duration_ms=100), model_call(duration_ms=200)],
    )
    report = _build(capsule_dir, metric="latency", since=SINCE, until=UNTIL)
    assert report["stat"] == "p95"
    assert report["unit"] == {"kind": "duration_ms", "stat": "p95"}
    # linear-interpolation p95 over [100, 200]
    assert report["series"][1]["value"] == pytest.approx(195.0)


def test_latency_mean_stat(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule(
        "run-a", created_at="2026-07-10T08:00:00Z",
        model_calls=[model_call(duration_ms=100), model_call(duration_ms=300)],
    )
    report = _build(capsule_dir, metric="latency", stat="mean", since=SINCE, until=UNTIL)
    assert report["stat"] == "mean"
    assert report["series"][1]["value"] == pytest.approx(200.0)


# ── request validation (usage errors, no capsule read) ───────────────────────


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"metric": "tokens"}, "unknown metric"),
        ({"metric": "score:"}, "unknown metric"),
        ({"metric": "cost", "group_by": "month"}, "unknown group-by"),
        ({"metric": "cost", "stat": "p95"}, "only valid with --metric latency"),
        ({"metric": "latency", "stat": "p42"}, "unknown stat"),
        ({"metric": "cost", "since": "not-a-window"}, "since"),
    ],
)
def test_invalid_requests_are_usage_errors(
    capsule_dir: Path, kwargs: dict, match: str
) -> None:
    with pytest.raises(TrendUsageError, match=match):
        build_trend_report(capsule_dir, now=NOW, **kwargs)


def test_missing_capsule_dir_is_runtime_error(tmp_path: Path) -> None:
    with pytest.raises(TrendError, match="capsule directory not found"):
        build_trend_report(tmp_path / "nope", metric="cost", now=NOW)


# ── asset grouping ────────────────────────────────────────────────────────────


def test_asset_grouping_is_categorical(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z",
                 metadata={"asset": "beta"}, model_calls=[model_call(cost=0.02)])
    make_capsule("run-b", created_at="2026-07-11T08:00:00Z",
                 metadata={"asset": "alpha"}, model_calls=[model_call(cost=0.03)])
    make_capsule("run-c", created_at="2026-07-11T09:00:00Z",
                 model_calls=[model_call(cost=0.04)])  # no asset dimension
    report = _build(capsule_dir, metric="cost", group_by="asset",
                    since=SINCE, until=UNTIL)
    assert [p["bucket"] for p in report["series"]] == ["(none)", "alpha", "beta"]
    assert all(p["bucket_start"] is None for p in report["series"])
    assert [p["value"] for p in report["series"]] == [
        pytest.approx(0.04), pytest.approx(0.03), pytest.approx(0.02)
    ]


# ── skips: never abort, always tally ─────────────────────────────────────────


def test_empty_dir_succeeds_with_empty_series(capsule_dir: Path) -> None:
    report = _build(capsule_dir, metric="cost", since=SINCE, until=UNTIL)
    assert report["series"] == []
    assert report["capsule_count"] == 0
    assert report["skipped_count"] == 0
    assert any("no capsules matched the window" in w for w in report["warnings"])


def test_capsules_outside_window_yield_empty_series(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-old", created_at="2026-01-01T00:00:00Z",
                 model_calls=[model_call(cost=0.02)])
    report = _build(capsule_dir, metric="cost", since=SINCE, until=UNTIL)
    assert report["series"] == []
    assert report["capsule_count"] == 0


def test_unreadable_capsule_is_skipped_not_fatal(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z",
                 model_calls=[model_call(cost=0.02)])
    broken = capsule_dir / "run-broken"
    broken.mkdir()
    (broken / "capsule.yaml").write_text("{ not: valid: yaml: [")
    (capsule_dir / "not-a-capsule").mkdir()  # no manifest: ignored, not skipped
    (capsule_dir / "loose-file.txt").write_text("x")
    report = _build(capsule_dir, metric="cost", since=SINCE, until=UNTIL)
    assert report["capsule_count"] == 1
    assert report["skipped_count"] == 1
    assert any("run-broken" in w for w in report["warnings"])


def test_missing_metric_capsule_is_skipped_with_warning(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z",
                 model_calls=[model_call(cost=0.02)])
    make_capsule("run-nc", created_at="2026-07-10T09:00:00Z",
                 model_calls=[model_call(cost=None)])
    report = _build(capsule_dir, metric="cost", since=SINCE, until=UNTIL)
    assert report["capsule_count"] == 1
    assert report["skipped_count"] == 1
    assert any("no cost recorded" in w for w in report["warnings"])
    assert report["series"][1]["value"] == pytest.approx(0.02)


def test_foreign_currency_capsule_skipped_never_converted(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    eur_call = model_call(cost=0.5)
    eur_call["nova.cost"]["currency"] = "EUR"
    make_capsule("run-eur", created_at="2026-07-10T08:00:00Z", model_calls=[eur_call])
    make_capsule("run-usd", created_at="2026-07-10T09:00:00Z",
                 model_calls=[model_call(cost=0.02)])
    report = _build(capsule_dir, metric="cost", since=SINCE, until=UNTIL)
    assert report["capsule_count"] == 1
    assert report["skipped_count"] == 1
    assert report["series"][1]["value"] == pytest.approx(0.02)  # EUR never blended
    assert any("unresolvable cost currency" in w and "EUR" in w
               for w in report["warnings"])


def test_foreign_currency_capsule_still_counts_for_latency(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    eur_call = model_call(cost=0.5, duration_ms=120)
    eur_call["nova.cost"]["currency"] = "EUR"
    make_capsule("run-eur", created_at="2026-07-10T08:00:00Z", model_calls=[eur_call])
    report = _build(capsule_dir, metric="latency", since=SINCE, until=UNTIL)
    assert report["capsule_count"] == 1
    assert report["skipped_count"] == 0


# ── saved-view selector (ADR-0130) ────────────────────────────────────────────


def test_saved_view_supplies_capsule_selection(
    make_capsule: CapsuleFactory,
    capsule_dir: Path,
    model_call: RecordFactory,
    views_dir: Path,
) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z",
                 metadata={"asset": "alpha"}, model_calls=[model_call(cost=0.02)])
    make_capsule("run-b", created_at="2026-07-10T09:00:00Z",
                 metadata={"asset": "beta"}, model_calls=[model_call(cost=0.99)])
    save_view(
        SavedView(
            view_id="alpha-only",
            name="Alpha only",
            query={"select": ["count()"], "where": ["asset = alpha"]},
            created_at="2026-07-14T00:00:00Z",
        ),
        views_dir,
    )
    report = _build(capsule_dir, metric="cost", since=SINCE, until=UNTIL,
                    view="alpha-only", views_dir=views_dir)
    assert report["view"] == "alpha-only"
    assert report["filters"] == {"where": ["asset = alpha"]}
    assert report["capsule_count"] == 1
    assert report["series"][1]["value"] == pytest.approx(0.02)


def test_unknown_view_is_runtime_error(capsule_dir: Path, views_dir: Path) -> None:
    with pytest.raises(TrendError, match="no saved view named"):
        build_trend_report(capsule_dir, metric="cost", view="missing",
                           views_dir=views_dir, now=NOW)


def test_view_with_invalid_stored_query_is_runtime_error(
    capsule_dir: Path, views_dir: Path
) -> None:
    # Bypass save_view's fail-closed gate: a hand-edited view file on disk.
    (views_dir / "bad-view.yaml").write_text(
        "schema_version: 0.1.0\n"
        "view_id: bad-view\n"
        "name: Bad view\n"
        "created_at: '2026-07-14T00:00:00Z'\n"
        "query:\n"
        "  select: ['drop table']\n"
    )
    with pytest.raises(TrendError, match="invalid query"):
        build_trend_report(capsule_dir, metric="cost", view="bad-view",
                           views_dir=views_dir, now=NOW)


# ── JSON shape stability ──────────────────────────────────────────────────────


def test_report_top_level_shape_is_stable(
    make_capsule: CapsuleFactory, capsule_dir: Path, model_call: RecordFactory
) -> None:
    make_capsule("run-a", created_at="2026-07-10T08:00:00Z",
                 model_calls=[model_call(cost=0.02)])
    report = _build(capsule_dir, metric="cost", since=SINCE, until=UNTIL)
    assert set(report) == {
        "schema_version", "generated_at", "generator", "metric", "group_by",
        "window", "unit", "capsule_count", "skipped_count", "series", "warnings",
    }
    assert report["schema_version"] == "0.1.0"
    assert report["generator"].startswith("nova-trend/")
    assert report["generated_at"] == "2026-07-15T12:00:00Z"


def test_unknown_top_level_key_rejected_by_schema() -> None:
    """Schema-shape guard: the graduated schema stays closed at the top level."""
    base = {
        "schema_version": "0.1.0",
        "generated_at": "2026-07-15T12:00:00Z",
        "generator": "nova-trend/0.1.0",
        "metric": "cost",
        "group_by": "day",
        "window": {"since": SINCE, "until": UNTIL},
        "unit": {"kind": "currency", "currency": "USD"},
        "capsule_count": 0,
        "skipped_count": 0,
        "series": [],
    }
    _VALIDATOR.validate(base)
    assert list(_VALIDATOR.iter_errors({**base, "threshold": 1.0}))  # closed
    assert list(  # metric/unit conditional binding enforced
        _VALIDATOR.iter_errors({**base, "unit": {"kind": "score", "name": "gaia"}})
    )
