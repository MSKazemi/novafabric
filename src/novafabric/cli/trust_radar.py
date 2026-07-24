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
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

_MARK = {"ok": "[green]●[/green]", "warn": "[yellow]◐[/yellow]",
         "fail": "[red]○[/red]", "na": "[dim]·[/dim]"}
_VERDICT_STYLE = {"attested": "green", "partial": "yellow",
                  "critical": "red", "unsealed": "dim"}


def _render(radar: object, json_out: bool) -> None:
    """Render a radar and exit. Shared by the document and --capsule paths so
    the two can never disagree about presentation or exit code."""
    is_critical = radar.verdict.value == "critical"  # type: ignore[attr-defined]

    if json_out:
        print(json.dumps(radar.model_dump(mode="json"), indent=2))  # type: ignore[attr-defined]
        raise typer.Exit(1 if is_critical else 0)

    style = _VERDICT_STYLE.get(radar.verdict.value, "white")  # type: ignore[attr-defined]
    header = f"Trust radar: [{style}]{radar.verdict.value.upper()}[/{style}]"  # type: ignore[attr-defined]
    if radar.capsule_id:  # type: ignore[attr-defined]
        header += f"   capsule: {radar.capsule_id}"  # type: ignore[attr-defined]
    console.print(header)
    for axis in radar.axes:  # type: ignore[attr-defined]
        reach = "n/a" if axis.value is None else f"{axis.value:.2f}"
        mark = _MARK[axis.state.value]
        console.print(f"  {mark} {axis.label:<20} {reach:>4}  ({axis.state.value})")

    raise typer.Exit(1 if is_critical else 0)


def trust_radar_cmd(
    verification: Annotated[
        Optional[Path],
        typer.Argument(
            help="Path to a JSON object of the 7 verification guarantees. "
            "Omit when using --capsule."
        ),
    ] = None,
    capsule: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule",
            help="Capsule directory to derive the guarantees from, instead of "
            "supplying them by hand. Guarantees the capsule cannot evidence "
            "render as n/a rather than as failures.",
            exists=True,
            file_okay=False,
        ),
    ] = None,
    capsule_id: Annotated[
        str | None,
        typer.Option(
            "--capsule-id",
            help="Label the radar with this capsule id. NOTE: a label only — it "
            "selects nothing. Use --capsule to read from a capsule.",
        ),
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
      nova trust-radar --capsule .novafabric/runs/01HX.../
      nova trust-radar verify.json
      nova trust-radar verify.json --json --capsule-id run-42
    """
    from novafabric.trust.radar import build_trust_radar

    if capsule is not None:
        if verification is not None:
            err_console.print(
                "[red]Pass either a verification file or --capsule, not both.[/red]"
            )
            raise typer.Exit(2)
        from novafabric.trust.capsule_flags import flags_from_capsule

        flags = flags_from_capsule(capsule)
        capsule_id = capsule_id or capsule.name
        radar = build_trust_radar(flags, capsule_id=capsule_id)
        _render(radar, json_out)
        return
    if verification is None:
        err_console.print(
            "[red]Provide a verification file or --capsule <dir>.[/red]"
        )
        raise typer.Exit(2)

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
    _render(radar, json_out)
