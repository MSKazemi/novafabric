"""CLI command: ``nova migrate-schema``.

Batch migration of capsule directories from schema_version < 1.0.0 to 1.0.0.

Walk every capsule directory under --capsule-dir, inspect the manifest, and
apply any needed schema upgrades:

  - Add ``schema_version: "1.0.0"`` when absent or less than 1.0.0.
  - Rename ``event_log.jsonl`` → ``model-calls.jsonl`` (legacy v0 file name).
  - Add ``format_version: "1"`` to capsule metadata when absent.

Exit codes
----------
0 — all capsules migrated (or already at v1, nothing to do)
1 — one or more capsule migrations failed
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, List, Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()

# The sentinel version that triggers migration.
_V1 = "1.0.0"

# Legacy file that must be renamed to model-calls.jsonl.
_LEGACY_EVENT_LOG = "event_log.jsonl"
_MODEL_CALLS = "model-calls.jsonl"


# ---------------------------------------------------------------------------
# Internal data classes
# ---------------------------------------------------------------------------


@dataclass
class _CapsuleResult:
    capsule_id: str
    old_version: str
    action: str
    result: str
    failed: bool = False


@dataclass
class _MigrationReport:
    results: List[_CapsuleResult] = field(default_factory=list)

    @property
    def any_failed(self) -> bool:
        return any(r.failed for r in self.results)

    @property
    def migrated_count(self) -> int:
        return sum(1 for r in self.results if r.action != "skip")

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.action == "skip")


# ---------------------------------------------------------------------------
# Core migration logic (no typer dependency — testable in isolation)
# ---------------------------------------------------------------------------


def _is_v0(schema_version: Optional[str]) -> bool:
    """Return True when *schema_version* is absent or below 1.0.0."""
    if not schema_version:
        return True
    try:
        major = int(str(schema_version).split(".")[0])
        return major < 1
    except (ValueError, IndexError):
        return True


def migrate_capsule(
    capsule_dir: Path,
    *,
    dry_run: bool,
    backup: bool,
) -> _CapsuleResult:
    """Migrate a single capsule directory in-place.

    Returns a :class:`_CapsuleResult` describing what happened.
    Never raises — errors are captured into the result.
    """
    capsule_id = capsule_dir.name
    manifest_path = capsule_dir / "manifest.json"

    # ── Read manifest ─────────────────────────────────────────────────────
    if not manifest_path.exists():
        return _CapsuleResult(
            capsule_id=capsule_id,
            old_version="—",
            action="skip",
            result="no manifest.json",
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _CapsuleResult(
            capsule_id=capsule_id,
            old_version="—",
            action="error",
            result=f"invalid JSON: {exc}",
            failed=True,
        )

    schema_version: Optional[str] = manifest.get("schema_version")

    # ── Already at v1: skip ───────────────────────────────────────────────
    if not _is_v0(schema_version):
        return _CapsuleResult(
            capsule_id=capsule_id,
            old_version=schema_version or "—",
            action="skip",
            result="already v1",
        )

    old_version: str = schema_version or "(absent)"
    actions_taken: list[str] = []

    # ── Plan changes ──────────────────────────────────────────────────────
    manifest_changed = False
    new_manifest = dict(manifest)

    # 1. Add/update schema_version
    if _is_v0(new_manifest.get("schema_version")):
        new_manifest["schema_version"] = _V1
        manifest_changed = True
        actions_taken.append("schema_version→1.0.0")

    # 2. Add format_version to metadata block if absent
    if "metadata" in new_manifest and isinstance(new_manifest["metadata"], dict):
        if "format_version" not in new_manifest["metadata"]:
            new_manifest["metadata"]["format_version"] = "1"
            manifest_changed = True
            actions_taken.append("metadata.format_version→1")
    elif "format_version" not in new_manifest:
        new_manifest["format_version"] = "1"
        manifest_changed = True
        actions_taken.append("format_version→1")

    # 3. Check for legacy event_log.jsonl
    legacy_path = capsule_dir / _LEGACY_EVENT_LOG
    model_calls_path = capsule_dir / _MODEL_CALLS
    rename_event_log = legacy_path.exists() and not model_calls_path.exists()
    if rename_event_log:
        actions_taken.append(f"{_LEGACY_EVENT_LOG}→{_MODEL_CALLS}")

    if not actions_taken:
        return _CapsuleResult(
            capsule_id=capsule_id,
            old_version=old_version,
            action="skip",
            result="nothing to migrate",
        )

    action_summary = ", ".join(actions_taken)

    # ── Dry run: report without touching files ────────────────────────────
    if dry_run:
        return _CapsuleResult(
            capsule_id=capsule_id,
            old_version=old_version,
            action=f"[dry-run] {action_summary}",
            result="would migrate",
        )

    # ── Apply changes ─────────────────────────────────────────────────────
    try:
        if manifest_changed:
            if backup:
                shutil.copy2(manifest_path, manifest_path.with_suffix(".json.v0.bak"))
            manifest_path.write_text(
                json.dumps(new_manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        if rename_event_log:
            if backup:
                shutil.copy2(legacy_path, legacy_path.with_suffix(".jsonl.v0.bak"))
            legacy_path.rename(model_calls_path)

    except OSError as exc:
        return _CapsuleResult(
            capsule_id=capsule_id,
            old_version=old_version,
            action=action_summary,
            result=f"error: {exc}",
            failed=True,
        )

    return _CapsuleResult(
        capsule_id=capsule_id,
        old_version=old_version,
        action=action_summary,
        result="migrated",
    )


def migrate_capsule_dir(
    capsule_dir: Path,
    *,
    dry_run: bool,
    backup: bool,
) -> _MigrationReport:
    """Walk *capsule_dir* and migrate every capsule sub-directory found."""
    report = _MigrationReport()

    if not capsule_dir.exists():
        return report  # nothing to migrate

    for entry in sorted(capsule_dir.iterdir()):
        if entry.is_dir():
            result = migrate_capsule(entry, dry_run=dry_run, backup=backup)
            report.results.append(result)

    return report


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def migrate_schema_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Option(
            "--capsule-dir",
            help=(
                "Root directory that contains one sub-directory per capsule. "
                "Default: ~/.novafabric/capsules/"
            ),
            show_default=False,
        ),
    ] = Path.home() / ".novafabric" / "capsules",
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would change without writing any files.",
        ),
    ] = False,
    backup: Annotated[
        bool,
        typer.Option(
            "--backup",
            help=(
                "Copy original files to <file>.v0.bak before overwriting. "
                "Allows manual rollback."
            ),
        ),
    ] = False,
) -> None:
    """Batch-upgrade capsule directories from schema v0 to the current format.

    Sets schema_version, renames event_log.jsonl, and adds format_version.
    Use --dry-run to preview changes before applying them. Use --backup to
    preserve originals.

    Scope: a directory of capsules.

    \b
    Examples:
      # Preview changes without applying them
      nova migrate-schema --dry-run capsules/

      # Apply with backup copies preserved
      nova migrate-schema --backup capsules/
    """
    if dry_run:
        console.print(
            f"[bold yellow]Dry run[/bold yellow] — reading from "
            f"[cyan]{capsule_dir}[/cyan] (no files will be changed)"
        )
    else:
        console.print(
            f"[bold]Migrating capsules[/bold] in [cyan]{capsule_dir}[/cyan]"
            + (" [yellow](--backup enabled)[/yellow]" if backup else "")
        )

    report = migrate_capsule_dir(capsule_dir, dry_run=dry_run, backup=backup)

    if not report.results:
        console.print("[dim]No capsule directories found — nothing to do.[/dim]")
        raise typer.Exit(code=0)

    _print_report(report)

    # Summary line
    console.print()
    if dry_run:
        console.print(
            f"[yellow]Dry run complete.[/yellow] "
            f"{report.migrated_count} capsule(s) would be migrated, "
            f"{report.skipped_count} already at v1."
        )
    else:
        _fail_color = "red" if report.any_failed else "green"
        _failed_count = sum(1 for r in report.results if r.failed)
        console.print(
            f"Migrated: [bold green]{report.migrated_count}[/bold green]  "
            f"Skipped: [dim]{report.skipped_count}[/dim]  "
            f"Failed: [bold {_fail_color}]{_failed_count}[/bold {_fail_color}]"
        )

    raise typer.Exit(code=1 if report.any_failed else 0)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _print_report(report: _MigrationReport) -> None:
    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("Capsule ID")
    tbl.add_column("Old version")
    tbl.add_column("Action")
    tbl.add_column("Result")

    for r in report.results:
        result_style = "red" if r.failed else ("dim" if r.action == "skip" else "green")
        tbl.add_row(
            r.capsule_id,
            r.old_version,
            r.action,
            f"[{result_style}]{r.result}[/{result_style}]",
        )

    console.print()
    console.print(tbl)
