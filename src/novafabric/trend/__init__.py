"""Offline score/cost/latency trend reports over local capsules (ADR-0131).

A `nova trend` report is a read-only, offline **snapshot artifact**: one
metric (``cost``, ``score:<name>``, or ``latency``) bucketed by ``day`` /
``week`` / ``asset`` over the local capsule directory, rendered as canonical
``TrendReport`` JSON and optionally one self-contained static HTML file.
Spec: ``design/spec/trend-report-v0.md``. Status: **experimental**.
"""

from novafabric.trend.errors import TrendError, TrendUsageError
from novafabric.trend.html import render_trend_html, write_trend_html
from novafabric.trend.report import (
    DEFAULT_SINCE,
    DEFAULT_STAT,
    GROUP_BYS,
    LATENCY_STATS,
    MAX_BUCKETS,
    REPORT_CURRENCY,
    TREND_SCHEMA_VERSION,
    build_trend_report,
)

__all__ = [
    "DEFAULT_SINCE",
    "DEFAULT_STAT",
    "GROUP_BYS",
    "LATENCY_STATS",
    "MAX_BUCKETS",
    "REPORT_CURRENCY",
    "TREND_SCHEMA_VERSION",
    "TrendError",
    "TrendUsageError",
    "build_trend_report",
    "render_trend_html",
    "write_trend_html",
]
