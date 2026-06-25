# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""CLI commands for examiner-mode evidence packet exporters.

  nova export-examiner bagit     — RFC 8493 BagIt preservation package
  nova export-examiner pccp      — FDA Predetermined Change Control Plan
  nova export-examiner iso42001  — ISO/IEC 42001 AI Management System package
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()

app = typer.Typer(
    help="Export examiner-mode evidence packages (BagIt, FDA PCCP, ISO 42001).",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# nova export-examiner bagit
# ---------------------------------------------------------------------------


@app.command("bagit")
def export_bagit(
    capsule_id: str = typer.Argument(
        ...,
        help="Capsule / run identifier (used as bag name and External-Identifier).",
    ),
    output_dir: Path = typer.Option(
        ...,
        "--output-dir",
        "-o",
        help="Directory in which to write <capsule_id>-bag.zip.",
    ),
    data_dir: Path = typer.Option(
        ...,
        "--data-dir",
        "-d",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Capsule data directory containing capsule.yaml and payload files.",
    ),
    contact_email: str = typer.Option(
        "",
        "--contact-email",
        help="Optional contact email written to bag-info.txt.",
    ),
    no_audit_report: bool = typer.Option(
        False,
        "--no-audit-report",
        help="Exclude audit-report.json from the bag payload even if present.",
    ),
) -> None:
    """Export a BagIt-packaged evidence archive from a capsule.

    BagIt (RFC 8493) is a standard for digital preservation and evidence
    exchange used by forensic examiners and archivists.

    Scope: single capsule.

    \b
    Examples:
      nova export-examiner bagit path/to/my-capsule/
      nova export-examiner bagit path/to/my-capsule/ --output evidence.bag.zip
    """
    try:
        from novafabric.compliance.export.examiner import BagItExporter
    except ImportError as exc:
        console.print(f"[red]✗[/red] examiner module not available: {exc}")
        raise typer.Exit(code=1) from exc

    exporter = BagItExporter(capsule_id=capsule_id, data_dir=data_dir)
    try:
        bag_path = exporter.export(
            output_dir=output_dir,
            include_audit_report=not no_audit_report,
            contact_email=contact_email,
        )
    except Exception as exc:
        console.print(f"[red]✗[/red] BagIt export failed: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]✓[/green] BagIt package written: {bag_path} "
        f"({bag_path.stat().st_size} bytes)"
    )


# ---------------------------------------------------------------------------
# nova export-examiner pccp
# ---------------------------------------------------------------------------


@app.command("pccp")
def export_pccp(
    baseline: str = typer.Option(
        ...,
        "--baseline",
        "-b",
        help="Baseline capsule ID (the current / approved version).",
    ),
    proposed: str = typer.Option(
        ...,
        "--proposed",
        "-p",
        help="Proposed capsule ID (the modification to be evaluated).",
    ),
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="Output path for the PCCP JSON file.",
    ),
    data_dir: Path = typer.Option(
        ...,
        "--data-dir",
        "-d",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Directory containing capsule sub-directories (or the capsule dir itself).",
    ),
) -> None:
    """Export an FDA Predetermined Change Control Plan package from a capsule.

    Generates a structured PCCP document per FDA guidance for AI/ML-based
    software as a medical device (SaMD).

    Scope: single capsule.

    \b
    Examples:
      nova export-examiner fda-pccp path/to/my-capsule/
    """
    try:
        from novafabric.compliance.export.examiner import PCCPExporter
    except ImportError as exc:
        console.print(f"[red]✗[/red] examiner module not available: {exc}")
        raise typer.Exit(code=1) from exc

    exporter = PCCPExporter(
        baseline_capsule_id=baseline,
        proposed_capsule_id=proposed,
        data_dir=data_dir,
    )
    try:
        out_path = exporter.export(output_path=output)
    except Exception as exc:
        console.print(f"[red]✗[/red] PCCP export failed: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]✓[/green] PCCP document written: {out_path} "
        f"({out_path.stat().st_size} bytes)"
    )


# ---------------------------------------------------------------------------
# nova export-examiner iso42001
# ---------------------------------------------------------------------------


@app.command("iso42001")
def export_iso42001(
    output: Path = typer.Option(
        ...,
        "--output",
        "-o",
        help="Output path for the ISO 42001 evidence package ZIP.",
    ),
    data_dir: Path = typer.Option(
        ...,
        "--data-dir",
        "-d",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Directory containing capsule data (used to build capsule index).",
    ),
    period: str = typer.Option(
        ...,
        "--period",
        help=(
            "Audit period in YYYY/YYYY-MM-DD format: "
            "e.g. '2026-01-01/2026-06-30' (ISO 8601 interval)."
        ),
    ),
    controls: Optional[list[str]] = typer.Option(  # noqa: UP007
        None,
        "--control",
        "-c",
        help=(
            "ISO 42001 clause number(s) to include, e.g. '6.1' '8.1'. "
            "Repeat the flag for multiple clauses. "
            "Defaults to all supported clauses."
        ),
    ),
) -> None:
    """Export an ISO/IEC 42001 AI Management System compliance package.

    Scope: single capsule.

    \b
    Examples:
      nova export-examiner iso42001 path/to/my-capsule/
    """
    try:
        from novafabric.compliance.export.examiner import ISO42001Exporter
    except ImportError as exc:
        console.print(f"[red]✗[/red] examiner module not available: {exc}")
        raise typer.Exit(code=1) from exc

    # Parse ISO 8601 interval: YYYY-MM-DD/YYYY-MM-DD
    try:
        parts = period.split("/")
        if len(parts) != 2:  # noqa: PLR2004
            raise ValueError("period must be in YYYY-MM-DD/YYYY-MM-DD format")
        period_start = datetime.fromisoformat(parts[0]).replace(tzinfo=timezone.utc)
        period_end = datetime.fromisoformat(parts[1]).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as exc:
        console.print(
            f"[red]✗[/red] Invalid period format: {exc}. "
            "Use YYYY-MM-DD/YYYY-MM-DD (e.g. 2026-01-01/2026-06-30)."
        )
        raise typer.Exit(code=1) from exc

    exporter = ISO42001Exporter(
        data_dir=data_dir,
        period_start=period_start,
        period_end=period_end,
    )
    try:
        out_path = exporter.export(output_path=output, controls=controls or None)
    except Exception as exc:
        console.print(f"[red]✗[/red] ISO 42001 export failed: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]✓[/green] ISO 42001 evidence package written: {out_path} "
        f"({out_path.stat().st_size} bytes)"
    )
