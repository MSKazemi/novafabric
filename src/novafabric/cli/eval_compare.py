from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from novafabric.evals._stats import RegressionDetector
from novafabric.evals.result import EvalResult

console = Console()
err_console = Console(stderr=True)


def eval_compare_cmd(
    baseline_path: Annotated[
        Path,
        typer.Argument(help="Path to the baseline EvalResult JSON file"),
    ],
    candidate_path: Annotated[
        Path,
        typer.Argument(help="Path to the candidate EvalResult JSON file"),
    ],
    alpha: Annotated[
        float,
        typer.Option(
            "--alpha",
            help="Significance level / relative-delta threshold (default 0.05)",
        ),
    ] = 0.05,
    min_samples: Annotated[
        int,
        typer.Option("--min-samples", help="Minimum sample_size to enable z-test (default 5)"),
    ] = 5,
) -> None:
    """Compare two EvalResult JSON files and report regressions.

    Exits 1 if a regression is detected (any score decreased). Exits 0
    if results are identical or improved.

    Scope: two eval result files.

    \b
    Examples:
      # Compare baseline to current
      nova eval compare baseline.json current.json

      # Use in CI (exits 1 on regression)
      nova eval compare baseline.json current.json && echo "No regression"
    """
    for label, path in (("baseline", baseline_path), ("candidate", candidate_path)):
        if not path.exists():
            err_console.print(f"[red]Error: {label} file not found: {path}[/red]")
            raise typer.Exit(code=1)

    try:
        baseline = EvalResult.model_validate_json(baseline_path.read_text())
    except Exception as exc:  # noqa: BLE001
        err_console.print(f"[red]Error parsing baseline JSON: {exc}[/red]")
        raise typer.Exit(code=1)

    try:
        candidate = EvalResult.model_validate_json(candidate_path.read_text())
    except Exception as exc:  # noqa: BLE001
        err_console.print(f"[red]Error parsing candidate JSON: {exc}[/red]")
        raise typer.Exit(code=1)

    detector = RegressionDetector(alpha=alpha, min_samples=min_samples)
    report = detector.compare(baseline=baseline, candidate=candidate)

    # ── Header info ───────────────────────────────────────────────────────────
    header = Table(title="Regression Comparison", show_header=True, header_style="bold")
    header.add_column("Field")
    header.add_column("Value")
    header.add_row("suite_id", report.suite_id)
    header.add_row("baseline_run_at", report.baseline_run_at.isoformat())
    header.add_row("candidate_run_at", report.candidate_run_at.isoformat())
    header.add_row("alpha", str(alpha))
    status_label = (
        "[red]✗ REGRESSION DETECTED[/red]"
        if report.regression_detected
        else "[green]✓ NO REGRESSION[/green]"
    )
    header.add_row("result", status_label)
    console.print(header)

    # ── Metrics table ─────────────────────────────────────────────────────────
    if report.metrics:
        metrics_table = Table(title="Metric Comparisons", show_header=True, header_style="bold")
        metrics_table.add_column("Metric")
        metrics_table.add_column("Baseline", justify="right")
        metrics_table.add_column("Candidate", justify="right")
        metrics_table.add_column("Delta", justify="right")
        metrics_table.add_column("Rel Δ%", justify="right")
        metrics_table.add_column("p-value", justify="right")
        metrics_table.add_column("Significant")

        for mc in report.metrics:
            delta_str = f"{mc.delta:+.4f}"
            rel_str = f"{mc.relative_delta * 100:+.2f}%"
            p_str = f"{mc.p_value:.4f}" if mc.p_value is not None else "n/a"
            sig_label = "[red]YES[/red]" if mc.significant else "no"
            metrics_table.add_row(
                mc.name,
                f"{mc.baseline_value:.4f}",
                f"{mc.candidate_value:.4f}",
                delta_str,
                rel_str,
                p_str,
                sig_label,
            )

        console.print(metrics_table)
    else:
        console.print("[yellow]No metrics in common between baseline and candidate.[/yellow]")

    console.print(f"\n{report.summary}")

    if report.regression_detected:
        sys.exit(1)
