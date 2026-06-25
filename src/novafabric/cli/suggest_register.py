"""CLI command: nova suggest-register.

Analyzes one or more captured run capsules and suggests assets to register.
Three modes:
  Interactive (default)   — prompts per suggestion, y/n/e(dit)/s(kip)
  --draft-only            — write draft YAML files without registering
  --auto                  — register all suggestions above --min-confidence
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric._paths import default_capsule_dir
from novafabric.registry.suggestion_engine import RegistrationSuggestion, SuggestionEngine

console = Console()


def suggest_register_cmd(
    capsule_ref: Annotated[
        Optional[str],
        typer.Argument(
            help=(
                "Run ID or path to capsule directory. "
                "Omit to scan the 10 most recent capsules."
            )
        ),
    ] = None,
    output_dir: Annotated[
        Optional[Path],
        typer.Option("--output-dir", "-o", help="Directory for draft YAML files"),
    ] = None,
    draft_only: Annotated[
        bool,
        typer.Option("--draft-only", help="Write draft YAML files without registering"),
    ] = False,
    auto: Annotated[
        bool,
        typer.Option(
            "--auto",
            help="Register all suggestions above --min-confidence without prompting",
        ),
    ] = False,
    min_confidence: Annotated[
        float,
        typer.Option("--min-confidence", help="Minimum confidence score (0.0–1.0) for --auto mode"),
    ] = 0.8,
    skip_types: Annotated[
        Optional[str],
        typer.Option(
            "--skip-types",
            help="Comma-separated asset types to skip (e.g. prompt,agent)",
        ),
    ] = None,
    runs_dir: Annotated[
        Optional[Path],
        typer.Option("--runs-dir", help="Base directory containing capsules (default: $NOVAFABRIC_HOME/capsules)."),  # noqa: E501
    ] = None,
) -> None:
    """Analyse captured capsules and suggest missing asset registrations.

    Inspects capsule evidence to infer which assets (models, tools, datasets)
    were used and generates draft YAML specs for review. Inverts the
    normal "register first" workflow: capture first, let the registry fill
    from evidence.

    Scope: one or more capsules.

    \b
    Examples:
      # Suggest registrations from a single capsule (interactive)
      nova suggest-register <run-id>

      # Write draft YAML files without registering
      nova suggest-register --draft-only <run-id>

      # Auto-register all suggestions above 80% confidence
      nova suggest-register --auto --min-confidence 0.8 <run-id>
    """
    from novafabric.registry.store import get_db_path

    db_path = get_db_path()
    base_dir = runs_dir or default_capsule_dir()
    skip_set = {t.strip() for t in (skip_types or "").split(",") if t.strip()}

    engine = SuggestionEngine()

    if capsule_ref is not None:
        # Resolve: try as path first, then as run ID under base_dir
        candidate = Path(capsule_ref)
        if candidate.is_dir():
            capsule_dir = candidate
        else:
            capsule_dir = base_dir / capsule_ref
            if not capsule_dir.is_dir():
                console.print(f"[red]Capsule not found:[/red] {capsule_ref}")
                raise typer.Exit(1)
        suggestions = engine.analyze(capsule_dir, db_path=db_path)
    else:
        suggestions = engine.analyze_recent(base_dir, db_path=db_path, limit=10)

    # Apply skip-types filter
    suggestions = [s for s in suggestions if s.asset_type not in skip_set]

    if not suggestions:
        console.print("[green]No unregistered assets detected.[/green]")
        return

    _print_summary_table(suggestions)

    if draft_only:
        _write_drafts(suggestions, output_dir or Path(".novafabric/drafts"))
        return

    if auto:
        eligible = [s for s in suggestions if s.confidence >= min_confidence]
        if not eligible:
            console.print(
                f"[yellow]No suggestions with confidence ≥ {min_confidence:.1f}.[/yellow]"
            )
            return
        for s in eligible:
            _register_suggestion(s, output_dir, db_path)
        return

    # Interactive mode
    draft_dir = output_dir or Path(".novafabric/drafts")
    draft_dir.mkdir(parents=True, exist_ok=True)
    registered = 0
    skipped = 0
    for i, s in enumerate(suggestions, 1):
        header = (
            f"\n[bold][{i}/{len(suggestions)}][/bold] {s.asset_type}: "
            f"[cyan]{s.detected_name}[/cyan]  "
            f"({s.call_count} calls, confidence {s.confidence:.0%})"
        )
        console.print(header)
        for w in s.warnings:
            console.print(f"  [yellow]⚠[/yellow] {w}")
        choice = typer.prompt(
            "  Register? [y/n/e(dit)/s(kip)]",
            default="n",
        ).strip().lower()
        if choice == "y":
            _register_suggestion(s, draft_dir, db_path)
            registered += 1
        elif choice in ("e", "edit"):
            draft_path = _write_single_draft(s, draft_dir)
            console.print(f"  Draft written to: {draft_path}")
            _open_editor(draft_path)
            if typer.confirm("  Register from edited spec?", default=True):
                _register_from_file(draft_path, db_path)
                registered += 1
        else:
            skipped += 1
            console.print("  → Skipped")

    console.print(f"\nDone. {registered} registered, {skipped} skipped.")


def _print_summary_table(suggestions: list[RegistrationSuggestion]) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Type", style="cyan", width=10)
    table.add_column("Name", style="white")
    table.add_column("Version", style="dim")
    table.add_column("Calls", justify="right", style="yellow")
    table.add_column("Confidence", justify="right")
    for s in suggestions:
        table.add_row(
            s.asset_type,
            s.detected_name,
            s.detected_version,
            str(s.call_count),
            f"{s.confidence:.0%}",
        )
    console.print(table)


def _write_drafts(
    suggestions: list[RegistrationSuggestion], output_dir: Path
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for s in suggestions:
        path = _write_single_draft(s, output_dir)
        console.print(f"  Draft: {path}")
    console.print(f"\n{len(suggestions)} draft(s) written to {output_dir}/")
    console.print("Edit them, then run: nova register <file>")


def _write_single_draft(s: RegistrationSuggestion, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{s.asset_type}-{s.detected_name.replace('/', '-')}.yaml"
    path = output_dir / filename
    path.write_text(s.draft_spec_yaml)
    return path


def _register_suggestion(
    s: RegistrationSuggestion, draft_dir: Path | None, db_path: Path | None
) -> None:
    draft_dir = draft_dir or Path(".novafabric/drafts")
    draft_path = _write_single_draft(s, draft_dir)
    _register_from_file(draft_path, db_path)


def _register_from_file(spec_path: Path, db_path: Path | None) -> None:
    from novafabric.registry.service import DuplicateAssetError, register_asset
    from novafabric.spec.validator import SpecValidationError, validate_spec

    try:
        spec = validate_spec(spec_path)
        register_asset(spec, spec_path=spec_path, db_path=db_path)
        console.print(f"  [green]✓[/green] Registered {spec.name}@{spec.version}")
    except SpecValidationError as exc:
        console.print(f"  [red]Validation error:[/red] {exc}")
    except DuplicateAssetError:
        console.print("  [yellow]Already registered[/yellow] (skipped)")
    except Exception as exc:
        console.print(f"  [red]Error:[/red] {exc}")


def _open_editor(path: Path) -> None:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    try:
        subprocess.run([editor, str(path)], check=False)
    except FileNotFoundError:
        console.print(f"  [dim]Editor '{editor}' not found — edit {path} manually.[/dim]")
