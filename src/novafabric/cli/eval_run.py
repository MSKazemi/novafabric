from __future__ import annotations

import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.cli.eval_compare import eval_compare_cmd
from novafabric.eval.runner import run_evals
from novafabric.evals import EvalSuiteError, load_eval_suite
from novafabric.registry.service import AssetNotFoundError

console = Console()
err_console = Console(stderr=True)

eval_app = typer.Typer(
    name="eval",
    help="Evaluation suite commands (ADR-0033)",
    no_args_is_help=True,
)

# NF-002/NF-010 evidence-grade evaluation (ADR-0099, experimental): signed eval cards
# + evidence-grade scores. Additive nested groups — `nova eval card …` / `nova eval score …`.
from novafabric.cli.eval_card import card_app, score_app  # noqa: E402
from novafabric.cli.eval_contamination import contamination_check_cmd  # noqa: E402
from novafabric.cli.eval_offline import offline_cmd  # noqa: E402

eval_app.add_typer(card_app, name="card")
eval_app.add_typer(score_app, name="score")
eval_app.command("offline")(offline_cmd)
# NF-028 (ADR-0108): benchmark-contamination flag over dataset_provenance facets.
eval_app.command("contamination-check")(contamination_check_cmd)


@eval_app.command("agent")
def eval_agent_cmd(
    asset_ref: str = typer.Argument(..., help="agent@version"),
) -> None:
    """Run the legacy asset-based evaluation suite (v0.1 interface).

    Prefer `nova eval run` for new work.

    Scope: named asset.

    \b
    Examples:
      nova eval agent my-agent@v1 --suite smoke
    """
    if "@" not in asset_ref:
        console.print("[red]Must specify version: agent@version[/red]")
        raise typer.Exit(code=1)

    name, agent_version = asset_ref.rsplit("@", 1)

    try:
        result = run_evals(name, agent_version)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    status = "[green]PASS[/green]" if result["passed"] else "[red]FAIL[/red]"
    console.print(f"Eval result for {name}@{agent_version}: {status}")
    for suite in result.get("suites", []):
        icon = "✓" if suite["passed"] else "✗"
        console.print(f"  {icon} {suite['suite_name']}: {suite.get('reason', '')}")


eval_app.command("compare")(eval_compare_cmd)


@eval_app.command("run")
def eval_run_cmd(
    capsule_path: Annotated[
        Path,
        typer.Argument(help="Path to the run capsule directory"),
    ],
    suite: Annotated[
        str,
        typer.Option("--suite", "-s", help="Suite ID to run (e.g. novafabric-smoke-v1)"),
    ],
    config: Annotated[
        Optional[list[str]],
        typer.Option(
            "--config",
            "-c",
            help="key=value config pairs passed to the adapter",
        ),
    ] = None,
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Write EvalResult JSON to this file"),
    ] = None,
) -> None:
    """Run a standard evaluation suite against a capsule directory.

    Built-in suites: gaia, swe-bench, agentbench, mmlu, smoke.
    Each suite runs inside an OCI-pinned container. Promotion is
    blocked by policy if a regression is detected.

    Scope: single capsule.

    \b
    Examples:
      # Run the smoke suite (fast, no container required)
      nova eval run --suite smoke path/to/my-capsule/

      # Run GAIA and write results to a file
      nova eval run --suite gaia --output results.json path/to/my-capsule/
    """
    # Parse --config key=value pairs
    config_dict: dict[str, str] = {}
    for entry in config or []:
        if "=" not in entry:
            err_console.print(f"[red]Invalid --config entry (expected key=value): {entry!r}[/red]")
            raise typer.Exit(code=1)
        k, _, v = entry.partition("=")
        config_dict[k.strip()] = v.strip()

    # Load the adapter
    try:
        adapter = load_eval_suite(suite)
    except EvalSuiteError as exc:
        err_console.print(f"[red]Error: {exc}[/red]")
        raise typer.Exit(code=1)

    # Run the suite
    try:
        result = adapter.run(capsule_path, config_dict)
    except EvalSuiteError as exc:
        err_console.print(f"[red]Eval suite error: {exc}[/red]")
        raise typer.Exit(code=1)
    except Exception as exc:  # noqa: BLE001
        err_console.print(f"[red]Unexpected error running suite: {exc}[/red]")
        raise typer.Exit(code=1)

    # ── Rich summary table ────────────────────────────────────────────────────
    oci_label = result.oci_digest if result.oci_digest else "host-env"
    passed_label = "[green]✓ PASS[/green]" if result.passed else "[red]✗ FAIL[/red]"

    summary = Table(title="Eval Run Summary", show_header=True, header_style="bold")
    summary.add_column("Field")
    summary.add_column("Value")
    summary.add_row("suite_id", result.suite_id)
    summary.add_row("version", result.suite_version)
    summary.add_row("oci_digest", oci_label)
    summary.add_row("capsule_id", result.capsule_id)
    summary.add_row("result", passed_label)
    console.print(summary)

    metrics_table = Table(title="Metrics", show_header=True, header_style="bold")
    metrics_table.add_column("Metric")
    metrics_table.add_column("Value", justify="right")
    metrics_table.add_column("Threshold", justify="right")
    metrics_table.add_column("Unit")
    metrics_table.add_column("Passed")
    for m in result.metrics:
        passed_cell = "[green]✓[/green]" if m.passed else "[red]✗[/red]"
        metrics_table.add_row(m.name, str(m.value), str(m.threshold), m.unit, passed_cell)
    console.print(metrics_table)

    # ── Optional JSON output ──────────────────────────────────────────────────
    if output is not None:
        output.write_text(result.model_dump_json(indent=2))
        console.print(f"[dim]Result written to {output}[/dim]")

    # Exit code reflects pass/fail
    if not result.passed:
        sys.exit(1)


@eval_app.command("list")
def eval_list_cmd() -> None:
    """List all registered evaluation suite adapters.

    Scope: global.

    \b
    Examples:
      nova eval list
    """
    _EP_GROUP = "novafabric.eval_suites"
    eps = entry_points(group=_EP_GROUP)
    ep_list = list(eps)

    if not ep_list:
        console.print("[yellow]No eval suite adapters registered.[/yellow]")
        console.print(
            f"[dim]Entry-point group: {_EP_GROUP}[/dim]"
        )
        raise typer.Exit(code=0)

    tbl = Table(
        title="Registered Eval Suites",
        show_header=True,
        header_style="bold cyan",
    )
    tbl.add_column("Suite ID", style="cyan")
    tbl.add_column("Version", justify="right")
    tbl.add_column("OCI Digest")
    tbl.add_column("Entry Point")

    errors: list[str] = []
    for ep in sorted(ep_list, key=lambda e: e.name):
        try:
            raw = ep.load()
            instance = raw() if callable(raw) else raw
            suite_id = instance.suite_id()
            version = instance.version()
            oci = instance.oci_digest() or "[dim]host-env[/dim]"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ep.name}: {exc}")
            tbl.add_row(ep.name, "[red]error[/red]", "—", ep.value)
            continue
        tbl.add_row(suite_id, version, oci, ep.value)

    console.print(tbl)

    if errors:
        console.print("\n[red]Load errors:[/red]")
        for e in errors:
            console.print(f"  [red]•[/red] {e}")
