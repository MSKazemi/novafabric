from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.replay._engine import ReplayEngine
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._intervention import InterventionError


class ReplayMode(str, Enum):
    mocked = "mocked"
    forensic = "forensic"
    semantic = "semantic"
    exact = "exact"
    intervention = "intervention"

console = Console()


def replay_cmd(
    capsule: Annotated[
        Path,
        typer.Argument(
            help="Run capsule directory, or the run id printed by `nova capture`."
        ),
    ],
    mode: Annotated[
        ReplayMode,
        typer.Option("--mode", help="Replay mode.")
    ] = ReplayMode.mocked,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would execute without running")
    ] = False,
    allow_readonly: Annotated[
        bool,
        typer.Option("--allow-readonly", help="Allow read-only calls to pass through."),
    ] = False,
    allow_mutating: Annotated[
        bool,
        typer.Option("--allow-mutating", help="Allow mutating calls (writes/deletes)."),
    ] = False,
    allow_external_side_effects: Annotated[
        bool,
        typer.Option("--allow-external-side-effects", help="Allow any external side effects."),
    ] = False,
    allow_unknown_mutation: Annotated[
        bool,
        typer.Option("--allow-unknown-mutation", help="Allow calls of unknown mutation class."),
    ] = False,
    output_dir: Annotated[
        Path | None, typer.Option("--output-dir", "-o", help="Base dir for replay output")
    ] = None,
    intervention_file: Annotated[
        Path | None,
        typer.Option(
            "--intervention-file",
            help=(
                "InterventionSpec YAML for --mode intervention "
                "(experimental, ADR-0086)"
            ),
        ),
    ] = None,
) -> None:
    """Re-run a captured run against recorded or mocked LLM responses.

    Five modes control how outbound calls are handled:
      mocked       — all calls intercepted; responses served from the capsule record
      forensic     — calls execute live but nothing is written (read-only observation)
      semantic     — LLM outputs regenerated live; structural behaviour preserved
      exact        — strict bit-for-bit replay with hash verification
      intervention — experimental: substitute one captured event per an
                     InterventionSpec, re-execute downstream under mocked
                     semantics, emit a diffable counterfactual capsule

    Scope: single capsule.

    \b
    Examples:
      # By run id — the id `nova capture` printed
      nova replay 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # Or by path
      nova replay path/to/my-capsule/

      # Forensic mode — observe without writing
      nova replay --mode forensic 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # Dry-run: show what would execute
      nova replay --dry-run path/to/my-capsule/

      # Counterfactual: what if the model had answered differently?
      nova replay --mode intervention --intervention-file spec.yaml path/to/my-capsule/
    """
    try:
        capsule = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if mode is ReplayMode.intervention and intervention_file is None:
        console.print(
            "[red]--mode intervention requires --intervention-file[/red]"
        )
        raise typer.Exit(code=1)
    if intervention_file is not None and mode is not ReplayMode.intervention:
        console.print(
            "[red]--intervention-file only applies to --mode intervention[/red]"
        )
        raise typer.Exit(code=1)

    flags = ReplayFlags(
        mode=mode.value,
        dry_run=dry_run,
        allow_readonly=allow_readonly,
        allow_mutating=allow_mutating,
        allow_external_side_effects=allow_external_side_effects,
        allow_unknown_mutation=allow_unknown_mutation,
        output_dir=output_dir,
        intervention_file=intervention_file,
    )

    base = output_dir or (Path.cwd() / ".novafabric" / "replays")
    engine = ReplayEngine(capsule_dir=capsule, flags=flags, base_dir=base)
    try:
        result = engine.run()
    except InterventionError as exc:
        console.print(f"[red]✗ Intervention error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if result.status == "dry_run":
        dry_report_path = base / result.replay_id / "dry_run_report.txt"
        if dry_report_path.exists():
            console.print(dry_report_path.read_text(), end="")
        return

    status_icon = "[green]✓[/green]" if result.status == "success" else "[red]✗[/red]"
    result_path = base / result.replay_id
    console.print(
        f"{status_icon} Replay written: {result_path}  "
        f"(replay_id={result.replay_id}  mode={result.mode})"
    )

    if result.env_warnings:
        for w in result.env_warnings:
            console.print(f"  [yellow]⚠ {w.get('message', '')}[/yellow]")

    if result.status not in ("success", "dry_run"):
        raise typer.Exit(code=result.exit_code or 1)
