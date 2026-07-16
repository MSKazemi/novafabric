"""nova backup / nova restore — evidence-grade backup sets (ADR-0181, experimental).

``nova backup create`` builds a ``.tar.gz`` backup set: for the local profile,
a live-writer-safe registry snapshot, the capsule directories, and a
secret-redacted config; ``--profile pg`` adds a ``pg_dump --format=custom``
member (the DSN is never logged or stored — the manifest carries only the
redacted host/dbname). The manifest is DSSE-signed when a local NovaSeal
profile is configured, or honestly ``unsigned`` otherwise. Key material NEVER
enters a set (normative deny-filter).

``nova backup verify`` checks a set fully offline: every member hash against
the manifest, plus the DSSE signature when present. Exit 1 on any mismatch.

``nova restore`` (local profile) runs the spec's normative order: verify the
set → prepare the home → extract → migrate → replay crypto-shreds → run the
verification chain. Restore of a pg-dump set is the ``pg_restore`` runbook
(``docs/ops/backup-restore.md`` §1.2) — not automated in this slice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

app = typer.Typer(
    name="backup",
    help="Evidence-grade local backup sets: create, verify (experimental, ADR-0181).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("create")
def create(
    output: Annotated[
        Optional[Path],
        typer.Option(
            "--output",
            "-o",
            help="Target .tar.gz file (or existing directory). "
            "Default: ./nova-backup-<set_id>.tar.gz",
            show_default=False,
        ),
    ] = None,
    home: Annotated[
        Optional[Path],
        typer.Option(
            "--home",
            help="NovaFabric home to back up (default: NOVAFABRIC_HOME or ~/.novafabric).",
            show_default=False,
        ),
    ] = None,
    profile: Annotated[
        str,
        typer.Option(
            "--profile",
            help="Backup profile: 'local' (SQLite deployment) or 'pg' "
            "(adds a pg_dump --format=custom member).",
        ),
    ] = "local",
    dsn: Annotated[
        Optional[str],
        typer.Option(
            "--dsn",
            help="Postgres DSN for --profile pg (default: NOVA_DSN or "
            "NOVAFABRIC_POSTGRES_DSN). Never logged; the manifest stores only "
            "the redacted host/dbname.",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Create a backup set (registry, capsules, redacted config; pg adds pg_dump).

    The registry is snapshotted with the SQLite online-backup API, so a live
    writer is safe. Signing keys, tokens, and env values are excluded by a
    normative deny-filter — key material never travels in a backup set.
    Connection strings are treated as secrets: the DSN never appears in the
    set, the manifest, or any output.

    \b
    Examples:
      nova backup create
      nova backup create -o /mnt/backups/
      nova backup create -o nightly.tar.gz
      nova backup create --profile pg --dsn postgresql://…  -o pg-nightly.tar.gz
    """
    from novafabric._paths import nova_home
    from novafabric.backup.create import BackupCreateError, create_backup

    resolved_home = home or nova_home()
    resolved_output = output or Path.cwd()

    try:
        result = create_backup(
            resolved_output, home=resolved_home, profile=profile, dsn=dsn
        )
    except BackupCreateError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    manifest = result.manifest
    if manifest.signing_status == "signed":
        sig_line = "[green]signed[/green] (DSSE, NovaSeal local profile)"
    else:
        sig_line = f"[yellow]unsigned[/yellow] — {manifest.signing_detail}"

    console.print(f"[green]✓[/green] Backup set written: {result.archive_path}")
    console.print(f"  set_id:  {manifest.set_id}")
    console.print(f"  profile: {manifest.profile}")
    console.print(f"  members: {len(manifest.members)}")
    console.print(f"  signing: {sig_line}")
    console.print(f"[dim]Verify with: nova backup verify {result.archive_path}[/dim]")


@app.command("verify")
def verify(
    set_path: Annotated[
        Path,
        typer.Argument(
            help="Backup-set archive (.tar.gz) to verify offline.",
        ),
    ],
) -> None:
    """Verify a backup set offline against its manifest (exit 1 on any mismatch).

    Recomputes every member's SHA-256 and, when the set is signed, verifies the
    manifest's DSSE envelope. Requires no live deployment, network, or private
    keys.

    \b
    Examples:
      nova backup verify nova-backup-01J....tar.gz
    """
    from novafabric.backup.verify import BackupVerifyError, verify_backup

    try:
        result = verify_backup(set_path)
    except BackupVerifyError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"Set:     {result.set_id} ({result.profile})")
    console.print(f"Members: {len(result.ok_members)} ok / {len(result.checks)} total")

    if result.signature_verified is True:
        console.print("Signing: [green]DSSE signature verified[/green]")
    elif result.signature_verified is False:
        console.print("Signing: [red]DSSE signature INVALID[/red]")
    else:
        console.print("Signing: [yellow]unsigned[/yellow] (hash integrity only)")

    for check in result.mismatched:
        err_console.print(f"[red]✗ mismatch[/red] {check.path}")
    for check in result.missing:
        err_console.print(f"[red]✗ missing[/red]  {check.path}")
    for error in result.errors:
        err_console.print(f"[red]✗[/red] {error}")

    if not result.ok:
        err_console.print("[red]✗ Backup set verification FAILED[/red]")
        raise typer.Exit(code=1)
    console.print("[green]✓ Backup set verified[/green]")


def restore_cmd(
    set_path: Annotated[
        Path,
        typer.Argument(
            help="Backup-set archive (.tar.gz) to restore from (local profile).",
        ),
    ],
    home: Annotated[
        Optional[Path],
        typer.Option(
            "--home",
            help="Target NovaFabric home (default: NOVAFABRIC_HOME or ~/.novafabric).",
            show_default=False,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Restore into a non-empty home: existing data is moved aside "
            "into a timestamped .pre-restore-…/ directory (never deleted).",
        ),
    ] = False,
) -> None:
    """Restore a local-profile backup set, then run the verification chain.

    Normative order (ADR-0181 / backup-restore-v0 spec): verify the set →
    prepare the home → extract → migrate to head → replay crypto-shreds
    (shredded data stays shredded) → doctor storage check + seal log verify.
    The restore is complete ONLY when verification passes — there is no flag
    to skip it. Exit 1 on any failed step.

    Restoring a pg-dump set is not automated in this slice: use the
    pg_restore runbook (docs/ops/backup-restore.md §1.2).

    \b
    Examples:
      nova restore nova-backup-01J….tar.gz
      nova restore set.tar.gz --home /srv/novafabric --force
    """
    from novafabric._paths import nova_home
    from novafabric.backup.restore import RestoreError, restore_backup

    resolved_home = home or nova_home()

    try:
        result = restore_backup(set_path, home=resolved_home, force=force)
    except RestoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"Set:  {result.set_id} ({result.profile})")
    console.print(f"Home: {result.home}")
    if result.moved_aside:
        console.print(f"Pre-existing data moved aside: {result.moved_aside}")
    for step in result.steps:
        mark = "[green]✓[/green]" if step.ok else "[red]✗[/red]"
        console.print(f"  {mark} {step.name}: {step.detail}")

    if not result.ok:
        err_console.print(
            "[red]✗ Restore FAILED — verification did not pass; the restored "
            "deployment must not serve reads[/red]"
        )
        raise typer.Exit(code=1)
    console.print("[green]✓ Restore complete — verification chain passed[/green]")
