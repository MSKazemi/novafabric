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
"""nova audit — compliance control-mapping engine CLI."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.compliance.audit import (
    BUILT_IN_PROFILES,
    AuditEngine,
    load_profile,
)
from novafabric.compliance.audit.models import AuditReport

console = Console()

app = typer.Typer(
    name="audit",
    help="Map capsule evidence to regulatory controls and report coverage gaps.",
    no_args_is_help=True,
)

_PROFILE_HELP = (
    "Compliance profile ID: "
    + ", ".join(BUILT_IN_PROFILES)
    + ". Or a path to a custom profile YAML."
)

_DataDirArg = Annotated[
    Path,
    typer.Option("--data-dir", "-d", help="NovaFabric data root directory"),
]
_ProfileArg = Annotated[
    str,
    typer.Option("--profile", "-p", help=_PROFILE_HELP),
]


def _load_engine(data_dir: Path, profile: str) -> AuditEngine:
    """Resolve profile and return an :class:`AuditEngine`."""
    try:
        ctrl_profile = load_profile(profile)
    except ValueError as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(code=1) from exc
    return AuditEngine(data_dir=data_dir, profile=ctrl_profile)


def _status_colour(status: str) -> str:
    return {
        "covered": "green",
        "partial": "yellow",
        "missing": "red",
        "not_applicable": "dim",
    }.get(status, "white")


def _print_report_summary(report: AuditReport) -> None:
    console.print(
        f"\n[bold]Audit Report[/bold]  [dim]{report.report_id}[/dim]"
    )
    console.print(
        f"  Profile  : [cyan]{report.profile_name}[/cyan]  "
        f"([dim]{report.framework} {report.profile_id}[/dim])"
    )
    console.print(f"  Data dir : {report.data_dir}")
    console.print(f"  Capsules : {report.capsule_count}")
    console.print(
        f"  Controls : {report.total_controls} total  "
        f"[green]{report.covered_controls} covered[/green]  "
        f"[yellow]{report.partial_controls} partial[/yellow]  "
        f"[red]{report.missing_controls} missing[/red]"
    )
    score_pct = f"{report.overall_score * 100:.1f}%"
    colour = "green" if report.overall_score >= 0.8 else (
        "yellow" if report.overall_score >= 0.5 else "red"
    )
    console.print(f"  Score    : [{colour}]{score_pct}[/{colour}]")
    if report.evidence_seal_verified:
        console.print("  Seals    : [green]verified[/green]")
    console.print()


def _print_coverage_table(report: AuditReport) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Control", style="cyan", no_wrap=True)
    table.add_column("Title")
    table.add_column("Status", justify="center")
    table.add_column("Score", justify="right")
    table.add_column("Capsules", justify="right")

    for cov in report.coverages:
        colour = _status_colour(cov.status)
        table.add_row(
            cov.control_id,
            cov.control_title,
            f"[{colour}]{cov.status}[/{colour}]",
            f"{cov.coverage_score * 100:.0f}%",
            str(len(cov.capsule_ids)),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("map")
def audit_map(
    data_dir: _DataDirArg = Path.home() / "novafabric-data",
    profile: _ProfileArg = "nist-ai-rmf",
    capsule_id: Annotated[
        Optional[str],
        typer.Option("--capsule", help="Audit a single capsule by ID"),
    ] = None,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of a table"),
    ] = False,
) -> None:
    """Map capsule evidence to regulatory controls and report coverage gaps.

    Built-in profiles: nist-ai-rmf, eu-ai-act, gdpr, soc2, iso-42001, reproducibility.
    Exits 1 if any required control has no evidence.

    Scope: single capsule (or all capsules via --data-dir).

    \b
    Examples:
      # Audit a capsule against the EU AI Act profile
      nova audit map --profile eu-ai-act --capsule <capsule-id>

      # Audit all capsules in a data directory
      nova audit map --profile nist-ai-rmf --data-dir ~/novafabric-data/capsules/

      # Export the audit report as JSON
      nova audit map --profile gdpr --json --data-dir ~/novafabric-data/
    """
    engine = _load_engine(data_dir, profile)
    report = engine.scan()

    if capsule_id is not None:
        # Filter coverages to those that reference the requested capsule
        report = report.model_copy(
            update={
                "coverages": [
                    c for c in report.coverages if capsule_id in c.capsule_ids
                ]
            }
        )

    if output_json:
        typer.echo(
            report.model_dump_json(
                indent=2, by_alias=True, exclude_none=True
            )
        )
        return

    _print_report_summary(report)
    _print_coverage_table(report)


@app.command("report")
def audit_report(
    data_dir: _DataDirArg = Path.home() / "novafabric-data",
    profile: _ProfileArg = "nist-ai-rmf",
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Write report to file (default: stdout)"),
    ] = None,
    format: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: json-ld (default) or text",
        ),
    ] = "json-ld",
) -> None:
    """Generate a full compliance audit report (JSON-LD or text).

    Scope: all capsules under --data-dir.

    \b
    Examples:
      nova audit report --profile eu-ai-act

      # Write to a file
      nova audit report --profile gdpr --output report.json

      # Plain text output
      nova audit report --format text
    """
    engine = _load_engine(data_dir, profile)
    report = engine.scan()

    if format == "text":
        lines: list[str] = [
            f"Audit Report: {report.report_id}",
            f"Generated: {report.generated_at.isoformat()}",
            f"Profile: {report.profile_name} ({report.framework})",
            f"Data dir: {report.data_dir}",
            f"Capsules scanned: {report.capsule_count}",
            f"Overall score: {report.overall_score * 100:.1f}%",
            f"Controls: {report.total_controls} total, "
            f"{report.covered_controls} covered, "
            f"{report.partial_controls} partial, "
            f"{report.missing_controls} missing",
            "",
        ]
        for cov in report.coverages:
            lines.append(
                f"[{cov.status.upper():12s}] {cov.control_id}  {cov.control_title}"
            )
            if cov.evidence_missing:
                for m in cov.evidence_missing:
                    lines.append(f"  MISSING: {m}")
        content = "\n".join(lines)
    else:
        content = report.model_dump_json(indent=2, by_alias=True, exclude_none=True)

    if output is not None:
        output.write_text(content, encoding="utf-8")
        console.print(f"[green]ok[/green] Report written to {output}")
    else:
        typer.echo(content)


@app.command("verify")
def audit_verify(
    report_path: Annotated[
        Path,
        typer.Argument(help="Path to an audit report JSON-LD file"),
    ],
) -> None:
    """Verify the integrity of a signed audit report.

    Checks that the report is structurally valid JSON-LD and parses cleanly.

    Scope: single audit report file.

    \b
    Examples:
      nova audit verify path/to/audit-report.json
    """
    if not report_path.exists():
        console.print(f"[red]x[/red] Report file not found: {report_path}")
        raise typer.Exit(code=1)

    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        report = AuditReport.model_validate(data)
    except Exception as exc:
        console.print(f"[red]x[/red] Failed to parse audit report: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]ok[/green] Report {report.report_id} is valid.")
    console.print(
        f"  Profile  : {report.profile_name}  ({report.framework})"
    )
    console.print(f"  Generated: {report.generated_at.isoformat()}")
    console.print(
        f"  Score    : {report.overall_score * 100:.1f}%  "
        f"({report.covered_controls}/{report.total_controls} controls covered)"
    )
    if report.evidence_seal_verified:
        console.print("  Seals    : [green]verified[/green]")
    else:
        console.print("  Seals    : [dim]not present[/dim]")


@app.command("bundle")
def audit_bundle(
    data_dir: _DataDirArg = Path.home() / "novafabric-data",
    profile: _ProfileArg = "nist-ai-rmf",
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Output ZIP path"),
    ] = Path("audit-bundle.zip"),
) -> None:
    """Export audit evidence as a ZIP archive for a compliance profile.

    Includes the audit report and all contributing capsule evidence files.

    Scope: all capsules under --data-dir.

    \b
    Examples:
      nova audit bundle --profile eu-ai-act
      nova audit bundle --profile nist-ai-rmf --output nist-evidence.zip
    """
    engine = _load_engine(data_dir, profile)
    report = engine.scan()

    # Collect capsule directories contributing evidence
    capsules_root = data_dir / "capsules"
    contributing_ids: set[str] = set()
    for cov in report.coverages:
        contributing_ids.update(cov.capsule_ids)

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Write the audit report as audit-report.json at the root
        report_json = report.model_dump_json(indent=2, by_alias=True, exclude_none=True)
        zf.writestr("audit-report.json", report_json)

        # Include evidence files from each contributing capsule
        files_added = 0
        if capsules_root.is_dir():
            for capsule_dir in capsules_root.iterdir():
                if not capsule_dir.is_dir():
                    continue
                if capsule_dir.name not in contributing_ids:
                    continue
                for evidence_file in sorted(capsule_dir.rglob("*")):
                    if evidence_file.is_file():
                        rel = evidence_file.relative_to(capsule_dir)
                        arc_name = str(
                            Path("capsules") / capsule_dir.name / rel
                        )
                        zf.write(evidence_file, arc_name)
                        files_added += 1

    console.print(
        f"[green]ok[/green] Audit bundle written to [cyan]{output}[/cyan]  "
        f"({files_added} evidence files, {len(contributing_ids)} capsules)"
    )
    console.print(
        f"     Score: {report.overall_score * 100:.1f}%  "
        f"({report.covered_controls}/{report.total_controls} controls covered)"
    )


@app.command("coverage")
def audit_coverage(
    data_dir: _DataDirArg = Path.home() / "novafabric-data",
    profile: _ProfileArg = "nist-ai-rmf",
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            help="Minimum required coverage score (0.0–1.0). Exit 1 if below.",
            min=0.0,
            max=1.0,
        ),
    ] = 0.8,
) -> None:
    """Show regulatory control coverage for a data directory (CI gate).

    Exits 1 if the coverage score is below the threshold. Use in CI pipelines
    to gate promotion on compliance coverage.

    Scope: all capsules under --data-dir.

    \b
    Examples:
      nova audit coverage
      nova audit coverage --profile eu-ai-act --threshold 0.9
    """
    engine = _load_engine(data_dir, profile)
    report = engine.scan()

    score_pct = report.overall_score * 100
    threshold_pct = threshold * 100
    colour = "green" if report.overall_score >= threshold else "red"

    console.print(
        f"[{colour}]{score_pct:.1f}%[/{colour}]  coverage  "
        f"(threshold: {threshold_pct:.1f}%  |  "
        f"profile: {report.profile_name})"
    )
    console.print(
        f"  {report.covered_controls} covered  "
        f"{report.partial_controls} partial  "
        f"{report.missing_controls} missing  "
        f"/ {report.total_controls} total controls"
    )

    if report.overall_score < threshold:
        console.print(
            f"[red]FAIL[/red] Coverage {score_pct:.1f}% is below threshold {threshold_pct:.1f}%"
        )
        # Print missing controls for CI diagnosis
        missing = [c for c in report.coverages if c.status in ("missing", "partial")]
        if missing:
            console.print("[dim]Controls needing evidence:[/dim]")
            for cov in missing:
                colour = _status_colour(cov.status)
                console.print(
                    f"  [{colour}]{cov.status:8s}[/{colour}] "
                    f"{cov.control_id}  {cov.control_title}"
                )
        raise typer.Exit(code=1)

    console.print("[green]PASS[/green] Coverage meets threshold.")
