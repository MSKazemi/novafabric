"""`nova trend` — offline score/cost/latency trend reports (ADR-0131).

Experimental. Computes a time- or asset-bucketed series of one metric over
the local capsule directory via the ADR-0129 extraction/filter path and emits
canonical ``TrendReport`` JSON (default: stdout), optionally plus a single
self-contained static HTML file. Read-only, offline, non-blocking — a
snapshot artifact, never a live monitor (thresholds/alerts are the ADR-0136
budget gate's concern).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from novafabric._paths import default_capsule_dir
from novafabric.trend import (
    TrendError,
    TrendUsageError,
    build_trend_report,
    write_trend_html,
)


def trend_cmd(
    metric: Annotated[
        str,
        typer.Option(
            "--metric",
            help="Metric to bucket: cost, latency, or score:<name> (e.g. score:gaia).",
        ),
    ],
    group_by: Annotated[
        str,
        typer.Option(
            "--group-by",
            help="Bucket key: day or week (UTC calendar buckets) or asset (categorical).",
        ),
    ] = "day",
    since: Annotated[
        str,
        typer.Option(
            "--since",
            help="Start of the input window (inclusive): 30d, 24h, P30D, "
            "or an RFC 3339 timestamp.",
        ),
    ] = "30d",
    until: Annotated[
        Optional[str],
        typer.Option(
            "--until",
            help="End of the input window (exclusive), RFC 3339; default now.",
        ),
    ] = None,
    stat: Annotated[
        Optional[str],
        typer.Option(
            "--stat",
            help="Latency point statistic per bucket: p50, p95, p99, or mean "
            "(latency only; default p95).",
        ),
    ] = None,
    view: Annotated[
        Optional[str],
        typer.Option(
            "--view",
            help="Saved view (ADR-0130) whose where-clause selects the capsules.",
        ),
    ] = None,
    json_file: Annotated[
        Optional[Path],
        typer.Option(
            "--json",
            help="Write the TrendReport JSON to this file (default: stdout).",
        ),
    ] = None,
    html_file: Annotated[
        Optional[Path],
        typer.Option(
            "--html",
            help="Also write one self-contained static HTML file (inline SVG, "
            "no JS, zero external requests).",
        ),
    ] = None,
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help="Capsule storage directory (defaults to $NOVAFABRIC_CAPSULE_DIR).",
        ),
    ] = None,
    views_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--views-dir",
            help="Saved-views directory (defaults to .novafabric/views; "
            "override $NOVAFABRIC_VIEWS_DIR).",
        ),
    ] = None,
) -> None:
    """Bucket one metric over local capsules into a trend report (experimental).

    Offline and read-only: no server, no network, no writes to the capsule
    store. Gap buckets are emitted explicitly (value null); capsules missing
    the metric are skipped with a warning, never an abort (ADR-0131).

    \b
    Examples:
      nova trend --metric cost --group-by week --since 60d
      nova trend --metric score:gaia --since 14d --json trend.json
      nova trend --metric latency --stat p99 --group-by asset --html trend.html
      nova trend --metric cost --view prod-summarizer
    """
    base = (capsule_dir or default_capsule_dir()).resolve()
    try:
        report = build_trend_report(
            base,
            metric=metric,
            group_by=group_by,
            since=since,
            until=until,
            stat=stat,
            view=view,
            views_dir=views_dir,
        )
    except TrendUsageError as exc:
        typer.echo(f"Trend error: {exc}", err=True)
        raise SystemExit(2) from exc
    except TrendError as exc:
        typer.echo(f"Trend error: {exc}", err=True)
        raise SystemExit(1) from exc

    wrote_file = False
    if html_file is not None:
        written = write_trend_html(report, html_file)
        typer.echo(f"Wrote {written}")
        wrote_file = True
    if json_file is not None:
        json_file.parent.mkdir(parents=True, exist_ok=True)
        json_file.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        typer.echo(f"Wrote {json_file}")
        wrote_file = True
    if not wrote_file:
        typer.echo(json.dumps(report))
