"""nova trust-radar — project a capsule's verification output into a trust radar.

ADR-0173 (data slice), read-only. Loads a JSON object of the seven Trust-Layer guarantees
(``signature_ok``, ``timestamp_ok``, ``log_integrity_ok``, ``redaction_coverage``,
``secret_scan_clean``, ``policy_pass``, ``eval_gate_pass``) — for example the summarized
output of ``nova verify`` plus the evidence-bundle scan flags — and prints the fixed-axis
radar. This is the CLI/JSON half of feature F-05; the `web/` SVG glyph is future design.

Exit codes:
  0 — attested / partial / unsealed (informational);
  1 — critical: a seal-integrity anchor (signature or log-integrity) failed;
  2 — the input file is missing or malformed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

_MARK = {"ok": "[green]●[/green]", "warn": "[yellow]◐[/yellow]",
         "fail": "[red]○[/red]", "na": "[dim]·[/dim]"}
_VERDICT_STYLE = {"attested": "green", "partial": "yellow",
                  "critical": "red", "unsealed": "dim"}


def trust_radar_cmd(
    verification: Annotated[
        Path,
        typer.Argument(help="Path to a JSON object of the 7 verification guarantees."),
    ],
    capsule_id: Annotated[
        str | None,
        typer.Option("--capsule-id", help="Capsule id to label the radar with."),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the radar model as JSON."),
    ] = False,
) -> None:
    """Render a Trust Attestation Radar from a capsule's verification output.

    \b
    The input is a JSON object of the seven guarantees; any absent/null guarantee
    becomes an ``n/a`` axis (e.g. an unsealed capsule has no ``signature_ok``).

    \b
    Examples:
      nova trust-radar verify.json
      nova trust-radar verify.json --json --capsule-id run-42
    """
    from novafabric.trust.radar import build_trust_radar

    if not verification.exists():
        err_console.print(f"[red]Verification file not found:[/red] {verification}")
        raise typer.Exit(2)
    try:
        flags = json.loads(verification.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read verification file:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(flags, dict):
        err_console.print("[red]Verification file must be a JSON object.[/red]")
        raise typer.Exit(2)

    radar = build_trust_radar(flags, capsule_id=capsule_id)
    is_critical = radar.verdict.value == "critical"

    if json_out:
        print(json.dumps(radar.model_dump(mode="json"), indent=2))
        raise typer.Exit(1 if is_critical else 0)

    style = _VERDICT_STYLE.get(radar.verdict.value, "white")
    header = f"Trust radar: [{style}]{radar.verdict.value.upper()}[/{style}]"
    if radar.capsule_id:
        header += f"   capsule: {radar.capsule_id}"
    console.print(header)
    for axis in radar.axes:
        reach = "n/a" if axis.value is None else f"{axis.value:.2f}"
        mark = _MARK[axis.state.value]
        console.print(f"  {mark} {axis.label:<20} {reach:>4}  ({axis.state.value})")

    raise typer.Exit(1 if is_critical else 0)
