"""Static-HTML conformance tests for trend reports (ADR-0131 P3).

Guards the normative contract of ``design/spec/trend-report-v0.md``
§Static-HTML: exactly one self-contained file, zero external requests, no
executable JavaScript, a stdlib pre-rendered inline SVG chart with gaps as
visible breaks, and the exact TrendReport JSON embedded inline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from novafabric.trend import render_trend_html, write_trend_html

_BASE: dict[str, Any] = {
    "schema_version": "0.1.0",
    "generated_at": "2026-07-15T12:00:00Z",
    "generator": "nova-trend/0.59.0",
    "metric": "cost",
    "group_by": "day",
    "window": {"since": "2026-07-09T00:00:00Z", "until": "2026-07-13T00:00:00Z"},
    "unit": {"kind": "currency", "currency": "USD"},
    "capsule_count": 3,
    "skipped_count": 1,
    "series": [
        {"bucket": "2026-07-09", "value": 1.5, "n": 1,
         "bucket_start": "2026-07-09T00:00:00Z"},
        {"bucket": "2026-07-10", "value": 2.0, "n": 2,
         "bucket_start": "2026-07-10T00:00:00Z"},
        {"bucket": "2026-07-11", "value": None, "n": 0,
         "bucket_start": "2026-07-11T00:00:00Z"},
        {"bucket": "2026-07-12", "value": 3.0, "n": 1,
         "bucket_start": "2026-07-12T00:00:00Z"},
    ],
    "warnings": ["bucket 2026-07-11 is a gap"],
}


def _report(**overrides: Any) -> dict[str, Any]:
    return {**_BASE, **overrides}


def _embedded_json(html: str) -> dict[str, Any]:
    match = re.search(
        r'<script type="application/json" id="trend-report-data">\s*(.*?)\s*</script>',
        html,
        re.DOTALL,
    )
    assert match is not None, "embedded trend-report-data block missing"
    return json.loads(match.group(1))


# ── single-file / offline invariant (the hard contract) ──────────────────────


def test_exactly_one_file_no_sidecars(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    written = write_trend_html(_report(), out_dir / "trend.html")
    assert written == out_dir / "trend.html"
    assert [p.name for p in out_dir.iterdir()] == ["trend.html"]


def test_no_external_references() -> None:
    """Zero external requests: no URLs, no linked assets, no network APIs."""
    html = render_trend_html(_report())
    assert "http://" not in html
    assert "https://" not in html
    for forbidden in (
        "<link",  # no external stylesheets/fonts/icons
        "src=",  # no external (or any) script/img/iframe sources
        "href=",  # no anchors/links needed at all
        "@import",
        "url(",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "<iframe",
    ):
        assert forbidden not in html, f"external-capable construct found: {forbidden}"


def test_no_executable_javascript() -> None:
    """No-JS page: the only <script> is the inert application/json data block."""
    html = render_trend_html(_report())
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts == ['<script type="application/json" id="trend-report-data">']


def test_css_is_inline() -> None:
    assert "<style>" in render_trend_html(_report())


# ── embedded data ─────────────────────────────────────────────────────────────


def test_embedded_json_is_the_exact_report() -> None:
    report = _report()
    assert _embedded_json(render_trend_html(report)) == report


def test_embedded_json_cannot_break_out_of_script_block() -> None:
    """A '</script>' payload in report data must not terminate the data block."""
    report = _report(warnings=["</script><script>alert(1)"])
    html = render_trend_html(report)
    assert "<script>alert(1)" not in html
    assert _embedded_json(html)["warnings"] == ["</script><script>alert(1)"]


# ── chart rendering (pre-rendered inline SVG, stdlib only) ───────────────────


def test_time_series_renders_line_with_gap_break() -> None:
    html = render_trend_html(_report())
    assert "<svg" in html
    # The gap splits the line into two segments: [09,10] and [12] — one <path>
    # for the two-point run; the isolated point renders as a circle only.
    assert len(re.findall(r"<path ", html)) == 1
    # One circle per non-null point; the gap is never drawn (and never zero).
    assert len(re.findall(r"<circle ", html)) == 3


def test_asset_grouping_renders_bars() -> None:
    report = _report(
        group_by="asset",
        series=[
            {"bucket": "alpha@1.0", "value": 812.0, "n": 12, "bucket_start": None},
            {"bucket": "beta@2.0", "value": 1450.0, "n": 18, "bucket_start": None},
        ],
    )
    html = render_trend_html(report)
    assert len(re.findall(r"<rect ", html)) == 2
    assert "<path " not in html


def test_bar_chart_skips_null_buckets_and_truncates_long_labels() -> None:
    report = _report(
        group_by="asset",
        series=[
            # Flat values exercise the degenerate y-domain (high == low).
            {"bucket": "a-very-long-asset-identifier@1.0.0", "value": 5.0, "n": 1,
             "bucket_start": None},
            {"bucket": "empty-asset", "value": None, "n": 0, "bucket_start": None},
            {"bucket": "beta", "value": 5.0, "n": 2, "bucket_start": None},
        ],
    )
    html = render_trend_html(report)
    assert len(re.findall(r"<rect ", html)) == 2  # the null bucket draws no bar
    assert "a-very-long-asset…" in html  # truncated axis label
    assert "a-very-long-asset-identifier@1.0.0</code>" in html  # full in table


def test_zero_only_series_still_renders_chart() -> None:
    """An all-zero series is data, not a gap — the degenerate domain widens."""
    series = [{"bucket": "2026-07-09", "value": 0.0, "n": 1,
               "bucket_start": "2026-07-09T00:00:00Z"}]
    html = render_trend_html(_report(series=series))
    assert "<svg" in html
    assert len(re.findall(r"<circle ", html)) == 1


def test_all_null_asset_series_renders_empty_state() -> None:
    report = _report(
        group_by="asset",
        series=[{"bucket": "alpha", "value": None, "n": 0, "bucket_start": None}],
    )
    html = render_trend_html(report)
    assert "<svg" not in html
    assert "no data in window" in html


def test_bucket_labels_are_escaped() -> None:
    report = _report(
        group_by="asset",
        series=[{"bucket": "<b>evil</b>", "value": 1.0, "n": 1, "bucket_start": None}],
    )
    html = render_trend_html(report)
    assert "<b>evil</b>" not in html
    assert "&lt;b&gt;evil" in html


def test_empty_series_renders_empty_state_not_chart() -> None:
    report = _report(series=[], capsule_count=0, warnings=["no capsules matched"])
    html = render_trend_html(report)
    assert "<svg" not in html
    assert "no data in window" in html
    assert _embedded_json(html) == report


def test_all_gap_series_renders_empty_state() -> None:
    series = [
        {"bucket": "2026-07-09", "value": None, "n": 0,
         "bucket_start": "2026-07-09T00:00:00Z"},
        {"bucket": "2026-07-10", "value": None, "n": 0,
         "bucket_start": "2026-07-10T00:00:00Z"},
    ]
    html = render_trend_html(_report(series=series, capsule_count=0))
    assert "<svg" not in html
    assert "no data in window" in html


# ── rendered sections ─────────────────────────────────────────────────────────


def test_summary_and_series_table_rendered() -> None:
    html = render_trend_html(_report(view="prod-summarizer"))
    for expected in (
        "Trend — cost by day",
        "prod-summarizer",
        "3 contributing",
        "1 skipped",
        "2026-07-09",
        "bucket 2026-07-11 is a gap",
        "TrendReport schema 0.1.0",
        "not</strong> a live monitor",
    ):
        assert expected in html, f"missing rendered section: {expected}"
    # The gap row shows a gap marker, never a zero.
    assert ">gap</span>" in html
