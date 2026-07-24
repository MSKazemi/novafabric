"""Typed registry over the serve report builders (ADR-0200/0201).

One table describing every dashboard report: identity, audience, accepted
filters, how to run its builder, and — where a genuine series exists — how to
chart it. The export router (HTML/PDF) and the catalog endpoint read this
registry; the legacy per-report JSON/CSV routes in ``app.py`` are unchanged.

Charts are declared only where the rows really form a series (throughput,
cost-burn, single-suite eval regression, release comparison) — nothing is
fabricated for tabular-by-nature reports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from novafabric.serve import reports as _reports
from novafabric.viz.svg import (
    DEFAULT_PALETTE,
    svg_bar_chart,
    svg_line_chart,
    svg_multi_line_chart,
    svg_stacked_bar_chart,
)

ChartKind = Literal["line", "bar", "stacked-bar", "multi-line"]

Rows = list[dict[str, Any]]
Builder = Callable[[Path, Path | None, dict[str, str]], tuple[list[str], Rows]]


@dataclass(frozen=True)
class ReportChartSpec:
    """How to turn report rows into a chart, when one honestly exists."""

    kind: ChartKind
    x: str
    y: tuple[str, ...]
    title: str
    colors: tuple[str, ...] = DEFAULT_PALETTE
    # Chart only renders when every one of these filters is set (e.g. the
    # eval-regression line is only meaningful for a single suite).
    requires_filters: tuple[str, ...] = ()
    # Server-side derivation for per-event rows (R4): "count-by-day" groups
    # rows by the day prefix of the x column and counts them. With
    # ``derive_split`` set, each y name is a genuine value of that column and
    # counts only rows carrying it (a stacked series of real categories);
    # without it, y is a single series name counting all rows per day. Rows
    # are never synthesized — the chart is an aggregation of the table below
    # it. Derived charts are rendered only in the server-side HTML/PDF export;
    # the catalog advertises ``chart: null`` so the web preview (which maps
    # rows directly) never receives a spec it cannot honestly render.
    derive: Literal["count-by-day"] | None = None
    derive_split: str | None = None

    def _derived_series(self, rows: Rows) -> Rows:
        """Aggregate per-event rows into per-day count rows (oldest first)."""
        buckets: dict[str, dict[str, Any]] = {}
        for r in rows:
            day = str(r.get(self.x) or "")[:10]
            if not day:
                continue
            b = buckets.setdefault(day, {self.x: day, **dict.fromkeys(self.y, 0)})
            if self.derive_split is None:
                b[self.y[0]] += 1
            else:
                value = str(r.get(self.derive_split))
                if value in self.y:
                    b[value] += 1
        out = sorted(buckets.values(), key=lambda b: str(b[self.x]))
        # Drop empty days (all-zero buckets, e.g. only unmatched split values).
        return [b for b in out if any(b[k] for k in self.y)]

    def render(self, rows: Rows, filters: dict[str, str]) -> str | None:
        if any(not filters.get(f) for f in self.requires_filters):
            return None
        if self.derive == "count-by-day":
            rows = self._derived_series(rows)
        if not rows:
            return None
        if self.kind in ("line", "bar"):
            series = [{"bucket": str(r.get(self.x)), "value": r.get(self.y[0])} for r in rows]
            if self.kind == "line":
                # Builders order time-series newest-first; a line reads
                # left-to-right in time. ISO timestamps sort lexically.
                series.sort(key=lambda p: str(p["bucket"]))
                return svg_line_chart(series)
            return svg_bar_chart(series)
        series = [
            {
                "bucket": str(r.get(self.x)),
                "values": {k: r.get(k) for k in self.y},
            }
            for r in rows
        ]
        if self.kind == "stacked-bar":
            return svg_stacked_bar_chart(series, list(self.y), list(self.colors))
        return svg_multi_line_chart(series, list(self.y), list(self.colors))


@dataclass(frozen=True)
class ReportSpec:
    """One dashboard report: identity, filters, builder adapter, chart."""

    report_id: str
    title: str
    audience: Literal["Developer", "Ops", "Compliance", "Management"]
    run: Builder
    filter_keys: tuple[str, ...] = ()
    required_filters: tuple[str, ...] = ()
    chart: ReportChartSpec | None = field(default=None)

    def catalog_entry(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "title": self.title,
            "audience": self.audience,
            "filter_keys": list(self.filter_keys),
            "required_filters": list(self.required_filters),
            # Derived charts stay export-only: the web preview maps rows
            # directly and cannot aggregate, so advertising them would break
            # the "single source of truth for preview charts" contract.
            "chart": (
                {
                    "kind": self.chart.kind,
                    "x": self.chart.x,
                    "y": list(self.chart.y),
                    "colors": list(self.chart.colors),
                    "title": self.chart.title,
                    "requires_filters": list(self.chart.requires_filters),
                }
                if self.chart and self.chart.derive is None
                else None
            ),
        }


def _f(filters: dict[str, str], key: str) -> str | None:
    value = filters.get(key)
    return value if value else None


REPORTS: dict[str, ReportSpec] = {
    spec.report_id: spec
    for spec in [
        ReportSpec(
            report_id="run-history",
            title="Run History",
            audience="Developer",
            filter_keys=("from", "to", "status", "agent"),
            run=lambda cd, db, f: _reports.report_run_history(
                cd,
                _f(f, "from"),
                _f(f, "to"),
                _f(f, "status"),
                _f(f, "agent"),
                db_path=db,
            ),
        ),
        ReportSpec(
            report_id="cost-burn",
            title="Cost Burn by Agent",
            audience="Ops",
            filter_keys=("from", "to"),
            run=lambda cd, db, f: _reports.report_cost_burn(
                cd, _f(f, "from"), _f(f, "to"), db_path=db
            ),
            chart=ReportChartSpec(
                kind="bar", x="agent", y=("runs",), title="Runs by agent"
            ),
        ),
        ReportSpec(
            report_id="throughput",
            title="Throughput",
            audience="Ops",
            filter_keys=("from", "to", "resolution"),
            run=lambda cd, db, f: _reports.report_throughput(
                cd,
                _f(f, "from"),
                _f(f, "to"),
                _f(f, "resolution") or "1d",
                db_path=db,
            ),
            chart=ReportChartSpec(
                kind="stacked-bar",
                x="window",
                y=("successes", "failures"),
                colors=("#3987e5", "#d94f4f"),
                title="Successes / failures per window",
            ),
        ),
        ReportSpec(
            report_id="executive-summary",
            title="Executive Summary",
            audience="Management",
            filter_keys=("from", "to"),
            run=lambda cd, db, f: _reports.report_executive_summary(
                cd, _f(f, "from"), _f(f, "to"), db_path=db
            ),
        ),
        ReportSpec(
            report_id="evidence-inventory",
            title="Evidence Inventory",
            audience="Compliance",
            filter_keys=("from", "to"),
            run=lambda cd, db, f: _reports.report_evidence_inventory(
                _f(f, "from"), _f(f, "to")
            ),
        ),
        ReportSpec(
            report_id="eval-regression",
            title="Eval Regression",
            audience="Developer",
            filter_keys=("from", "to", "suite"),
            run=lambda cd, db, f: _reports.report_eval_regression(
                db, _f(f, "from"), _f(f, "to"), _f(f, "suite")
            ),
            chart=ReportChartSpec(
                kind="line",
                x="run_at",
                y=("score",),
                title="Score over time",
                requires_filters=("suite",),
            ),
        ),
        ReportSpec(
            report_id="policy-audit",
            title="Policy Audit",
            audience="Compliance",
            filter_keys=("from", "to", "policy_id", "result"),
            run=lambda cd, db, f: _reports.report_policy_audit(
                db, _f(f, "from"), _f(f, "to"), _f(f, "policy_id"), _f(f, "result")
            ),
        ),
        ReportSpec(
            report_id="seal-verification",
            title="Seal Verification",
            audience="Compliance",
            filter_keys=("from", "to"),
            run=lambda cd, db, f: _reports.report_seal_verification(
                db, _f(f, "from"), _f(f, "to")
            ),
        ),
        ReportSpec(
            report_id="capsule-compare",
            title="Capsule Compare",
            audience="Developer",
            filter_keys=("run_a", "run_b"),
            required_filters=("run_a", "run_b"),
            run=lambda cd, db, f: _reports.report_capsule_compare(
                cd, f.get("run_a", ""), f.get("run_b", "")
            ),
        ),
        ReportSpec(
            report_id="release-comparison",
            title="Release Comparison",
            audience="Management",
            filter_keys=("version_a", "version_b"),
            required_filters=("version_a", "version_b"),
            run=lambda cd, db, f: _reports.report_release_comparison(
                db, f.get("version_a", ""), f.get("version_b", "")
            ),
            chart=ReportChartSpec(
                kind="bar",
                x="suite_name",
                y=("delta",),
                title="Score delta by suite",
            ),
        ),
        # ── R4 enterprise templates ──────────────────────────────────────
        ReportSpec(
            report_id="alert-digest",
            title="Alert Digest",
            audience="Ops",
            filter_keys=("from", "to"),
            run=lambda cd, db, f: _reports.report_alert_digest(
                _f(f, "from"), _f(f, "to")
            ),
            # Delivered vs failed are genuine values of severity_or_outcome
            # on the delivery rows; emitted rows are outside this chart, which
            # is why the title says "deliveries", not "alerts".
            chart=ReportChartSpec(
                kind="stacked-bar",
                x="ts",
                y=("delivered", "failed"),
                colors=("#3987e5", "#d94f4f"),
                title="Alert deliveries per day: delivered vs failed",
                derive="count-by-day",
                derive_split="severity_or_outcome",
            ),
        ),
        ReportSpec(
            report_id="api-key-inventory",
            title="API Key Inventory",
            audience="Compliance",
            run=lambda cd, db, f: _reports.report_api_key_inventory(db),
            # No chart: `status` is derived (active/revoked/expired), not a
            # stored column, and a count-by-status bar would need synthesized
            # aggregate rows — honesty over decoration.
        ),
        ReportSpec(
            report_id="dashboard-audit",
            title="Dashboard Audit",
            audience="Compliance",
            filter_keys=("action",),
            run=lambda cd, db, f: _reports.report_dashboard_audit(
                _f(f, "action")
            ),
            chart=ReportChartSpec(
                kind="bar",
                x="ts",
                y=("actions",),
                title="Audit actions per day",
                derive="count-by-day",
            ),
        ),
        ReportSpec(
            report_id="compliance-posture",
            title="Compliance Posture (Annex IV)",
            audience="Management",
            filter_keys=("run_id", "deployment_id"),
            required_filters=("run_id",),
            run=lambda cd, db, f: _reports.report_compliance_posture(
                cd, f.get("run_id", ""), _f(f, "deployment_id")
            ),
            # No chart: rows are per-element and completeness_flag is
            # categorical; a count-by-flag bar would require synthesized
            # rows the current row mapping cannot express honestly.
        ),
    ]
}
