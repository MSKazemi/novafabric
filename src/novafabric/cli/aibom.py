from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric._paths import default_capsule_dir

console = Console()

aibom_app = typer.Typer(
    name="aibom",
    help="AI Bill of Materials (AI-SBOM) — CycloneDX ML-BOM v1.7 status and CRA compliance.",
    no_args_is_help=True,
)

_CRA_DEADLINE = "2026-09-11"
_SPEC_VERSION = "CycloneDX ML-BOM 1.7 (ECMA-424 2nd Edition)"
_REGULATION = "EU Cyber Resilience Act (Regulation 2024/2847)"

_AIBOM_FILENAME = "aibom.json"


def _count_capsule_coverage(capsules_dir: Path) -> tuple[int, int]:
    """Return (total_capsules, capsules_with_aibom)."""
    if not capsules_dir.is_dir():
        return 0, 0
    total = 0
    covered = 0
    for entry in capsules_dir.iterdir():
        if not entry.is_dir():
            continue
        if not (entry / "capsule.yaml").exists():
            continue
        total += 1
        if (entry / _AIBOM_FILENAME).exists():
            covered += 1
    return total, covered


@aibom_app.command("generate")
def aibom_generate_cmd(
    capsule_dir: Annotated[
        Optional[Path],
        typer.Argument(
            help="A single capsule directory to generate an AIBOM for. "
            "Omit and use --all to batch-process every capsule.",
        ),
    ] = None,
    all_capsules: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Generate aibom.json for every capsule under --capsules-dir "
            "that doesn't already have one (per-deployment automation).",
        ),
    ] = False,
    capsules_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsules-dir",
            help="Directory of capsules for --all. "
            "Default: $NOVAFABRIC_CAPSULE_DIR or $NOVAFABRIC_HOME/capsules.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Regenerate aibom.json even if one already exists.",
        ),
    ] = False,
    citations: Annotated[
        bool,
        typer.Option(
            "--citations",
            help="NF-056: bind each model/dataset component to its source capsule/evidence "
            "digest (+ ADR-0097 inclusion proof when present). Default off (byte-stable).",
        ),
    ] = False,
    tlp: Annotated[
        Optional[str],
        typer.Option(
            "--tlp",
            help="NF-056: TLP 2.0 distribution marker "
            "(TLP:CLEAR|GREEN|AMBER|AMBER+STRICT|RED). Default: none.",
        ),
    ] = None,
    model_card: Annotated[
        Optional[str],
        typer.Option(
            "--model-card",
            help="NF-056: add a model-card externalReference to each model component. "
            "'auto' derives a registry URI; or pass an explicit path/URI. Default: none.",
        ),
    ] = None,
    include_datasets: Annotated[
        bool,
        typer.Option(
            "--include-datasets/--no-include-datasets",
            help="Emit type:data dataset components from lineage (default on).",
        ),
    ] = True,
) -> None:
    """Generate CycloneDX 1.7 AI-BOM(s) for one or all capsules.

    Per-deployment automation for EU CRA compliance (deadline 2026-09-11):
    refresh the AI-SBOM across a whole capsule store in one pass instead of
    invoking ``nova export-aibom`` per capsule.

    Scope: single capsule (positional) or batch (--all).

    \b
    Examples:
      # One capsule
      nova aibom generate path/to/my-capsule/

      # Every capsule missing an aibom.json
      nova aibom generate --all

      # Refresh all (overwrite existing)
      nova aibom generate --all --force
    """
    from novafabric.compliance.export.aibom import TLP_MARKERS, AIBOMExporter

    if not all_capsules and capsule_dir is None:
        console.print(
            "[red]✗[/red] Provide a capsule directory or use [bold]--all[/bold]."
        )
        raise typer.Exit(code=2)

    if tlp is not None and tlp not in TLP_MARKERS:
        console.print(
            f"[red]✗[/red] Invalid --tlp {tlp!r}; expected one of {sorted(TLP_MARKERS)}."
        )
        raise typer.Exit(code=2)

    exporter = AIBOMExporter()

    def _generate_one(cap: Path) -> bool:
        """Return True if an AIBOM was written, False if skipped."""
        out_path = cap / _AIBOM_FILENAME
        if out_path.exists() and not force:
            return False
        doc = exporter.build_aibom(
            cap,
            citations=citations,
            tlp=tlp,
            model_card_ref=model_card,
            include_datasets=include_datasets,
        )
        exporter.export_json(doc, out_path)
        return True

    if all_capsules:
        resolved_dir = (capsules_dir or default_capsule_dir()).resolve()
        if not resolved_dir.is_dir():
            console.print(f"[red]✗[/red] Capsules directory not found: {resolved_dir}")
            raise typer.Exit(code=1)
        written = 0
        skipped = 0
        failed = 0
        for entry in sorted(resolved_dir.iterdir()):
            if not entry.is_dir() or not (entry / "capsule.yaml").exists():
                continue
            try:
                if _generate_one(entry):
                    written += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                console.print(f"[yellow]⚠[/yellow] {entry.name}: {exc}")
        console.print(
            f"[green]✓[/green] AI-SBOM generate: "
            f"{written} written, {skipped} skipped (already present), {failed} failed."
        )
        if failed:
            raise typer.Exit(code=1)
        return

    # Single capsule path.
    cap = capsule_dir.resolve()  # type: ignore[union-attr]
    if not (cap / "capsule.yaml").exists():
        console.print(f"[red]✗[/red] Not a valid capsule directory: {cap}")
        raise typer.Exit(code=1)
    try:
        wrote = _generate_one(cap)
    except Exception as exc:
        console.print(f"[red]✗[/red] AI-SBOM generation failed: {exc}")
        raise typer.Exit(code=1) from exc
    if wrote:
        console.print(f"[green]✓[/green] AI-SBOM written: {cap / _AIBOM_FILENAME}")
    else:
        console.print(
            f"[dim]AI-SBOM already exists at {cap / _AIBOM_FILENAME} "
            "(use --force to regenerate).[/dim]"
        )


@aibom_app.command("status")
def aibom_status_cmd(
    capsules_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsules-dir",
            help=(
                "Directory containing capsules. "
                "Default: $NOVAFABRIC_CAPSULE_DIR or $NOVAFABRIC_HOME/capsules."
            ),
        ),
    ] = None,
) -> None:
    """Show AI-SBOM compliance status and EU CRA readiness.

    Reports whether the capsule contains the AI component metadata required
    by the EU Cyber Resilience Act SBOM mandate (deadline 2026-09-11).

    Scope: single capsule.

    \b
    Examples:
      nova aibom status path/to/my-capsule/
    """
    resolved_dir = (capsules_dir or default_capsule_dir()).resolve()
    total, covered = _count_capsule_coverage(resolved_dir)

    if total == 0:
        coverage_str = "[dim]no capsules found[/dim]"
    elif covered == total:
        coverage_str = f"[green]{covered}/{total} (complete)[/green]"
    else:
        missing = total - covered
        coverage_str = f"[yellow]{covered}/{total} — {missing} capsule(s) missing AIBOM[/yellow]"

    table = Table(title="AI-SBOM / CRA Compliance Status", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row("Regulation", _REGULATION)
    table.add_row("CRA SBOM deadline", f"[bold red]{_CRA_DEADLINE}[/bold red]")
    table.add_row("Export format", _SPEC_VERSION)
    table.add_row("Capsule directory", str(resolved_dir))
    table.add_row("AIBOM coverage", coverage_str)
    console.print(table)

    if total > 0 and covered < total:
        console.print(
            "\n[dim]Run [bold]nova export-aibom <capsule_dir> --output "
            f"<capsule_dir>/{_AIBOM_FILENAME}[/bold] for each uncovered capsule.[/dim]"
        )


@aibom_app.command("validate")
def aibom_validate_cmd(
    bom_file: Annotated[
        Path,
        typer.Argument(help="Path to a CycloneDX 1.7 AI-BOM JSON file to validate."),
    ],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the validation result as JSON.")
    ] = False,
) -> None:
    """Validate a CycloneDX 1.7 ML-BOM (NF-056 structural + NovaFabric-binding check).

    Checks the required CycloneDX 1.7 fields (specVersion, bomFormat, serialNumber),
    TLP marker validity, and per-component binding (name/bom-ref/hash-alg/citation
    digest). Exits ``0`` if the BOM passes, ``1`` on validation errors, ``2`` on a
    missing/malformed file.

    Scope: single BOM file.

    \b
    Examples:
      nova aibom validate path/to/aibom.json
    """
    import json as _json

    from novafabric.compliance.export.aibom import AIBOMExporter

    if not bom_file.is_file():
        console.print(f"[red]✗[/red] Not a file: {bom_file}")
        raise typer.Exit(code=2)
    try:
        payload = _json.loads(bom_file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        console.print(f"[red]✗[/red] Could not parse BOM JSON: {exc}")
        raise typer.Exit(code=2) from exc
    if not isinstance(payload, dict):
        console.print("[red]✗[/red] BOM must be a JSON object.")
        raise typer.Exit(code=2)

    errors = AIBOMExporter.validate(payload)
    if as_json:
        console.print_json(_json.dumps({"valid": not errors, "errors": errors}))
    elif errors:
        console.print(f"[red]✗[/red] {len(errors)} validation error(s):")
        for err in errors:
            console.print(f"  [red]•[/red] {err}")
    else:
        console.print(f"[green]✓[/green] Valid CycloneDX 1.7 AI-BOM: {bom_file}")
    if errors:
        raise typer.Exit(code=1)
