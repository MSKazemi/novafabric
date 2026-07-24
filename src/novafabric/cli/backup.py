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
            help="Backup profile: 'local' (SQLite deployment), 'pg' "
            "(adds a pg_dump --format=custom member), or 'manifest' "
            "(WORM object-store deployments: chain heads + checkpoints, "
            "no blobs — ADR-0216 D6).",
        ),
    ] = "local",
    backend: Annotated[
        Optional[str],
        typer.Option(
            "--backend",
            help="Object-store backend for --profile manifest: "
            "local | s3 | minio | ceph_rgw | azure_blob.",
            envvar="NOVA_OCS_BACKEND",
            show_default=False,
        ),
    ] = None,
    tenant: Annotated[
        Optional[list[str]],
        typer.Option(
            "--tenant",
            help="Scope the manifest listing to these tenant(s) (repeatable).",
            show_default=False,
        ),
    ] = None,
    allow_pending_wal: Annotated[
        bool,
        typer.Option(
            "--allow-pending-wal",
            help="Proceed with --profile manifest even when the local WAL has "
            "pending un-chained uploads (the gap is recorded in the listing).",
        ),
    ] = False,
    deep: Annotated[
        bool,
        typer.Option(
            "--deep",
            help="--profile manifest: fully verify every chain at create time "
            "(read_chain integrity), not just pin the heads.",
        ),
    ] = False,
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
    include_keys: Annotated[
        bool,
        typer.Option(
            "--include-keys",
            help="ALSO pack the signing keyring and novaseal.yaml + its "
            "key/cert PEMs (ADR-0216 D4). Default off: key material never "
            "travels in a set unless explicitly opted in. A set created with "
            "this flag requires key-custody care.",
        ),
    ] = False,
) -> None:
    """Create a backup set covering every persistent local store (ADR-0216).

    Backs up the registry, capsules, incidents, metadata, PII DEK store,
    seal transparency log, TSA nonces, ratchet state, dashboard state, spool,
    the audit log, and a secret-redacted config. SQLite stores are snapshotted
    with the online-backup API, so live writers are safe. The signed manifest
    carries a coverage table — what was NOT captured is recorded, never
    silent. Signing keys are excluded unless --include-keys. Connection
    strings are treated as secrets: the DSN never appears in the set, the
    manifest, or any output.

    NOTE: the default set includes the PII DEK store (dek.db) so restored PII
    stays readable — treat backup sets as sensitive artifacts and store them
    encrypted at rest.

    \b
    Examples:
      nova backup create
      nova backup create -o /mnt/backups/
      nova backup create -o nightly.tar.gz --include-keys
      nova backup create --profile pg --dsn postgresql://…  -o pg-nightly.tar.gz
    """
    from novafabric._paths import nova_home
    from novafabric.backup.create import BackupCreateError, create_backup

    resolved_home = home or nova_home()
    resolved_output = output or Path.cwd()

    try:
        result = create_backup(
            resolved_output,
            home=resolved_home,
            profile=profile,
            dsn=dsn,
            include_keys=include_keys,
            backend=backend,
            tenants=tenant,
            allow_pending_wal=allow_pending_wal,
            deep=deep,
        )
    except BackupCreateError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        # ADR-0192 wired source: a backup you cannot take is a recoverability
        # guarantee already gone — `critical`.
        from novafabric.events.sources import emit_backup_failed_alert  # noqa: PLC0415

        emit_backup_failed_alert(
            operation="create", target=str(resolved_output), reason=str(exc)
        )
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
    if manifest.coverage:
        console.print("  coverage:")
        for entry in manifest.coverage:
            marks = {"included": "[green]✓[/green]", "absent": "[dim]-[/dim]"}
            mark = marks.get(entry.status, "[yellow]![/yellow]")
            line = f"    {mark} {entry.component}: {entry.status}"
            if entry.detail:
                line += f" — {entry.detail}"
            console.print(line)
    if manifest.includes_keys:
        console.print(
            "[yellow]⚠ This set contains KEY MATERIAL — store it with key-backup "
            "custody rules (docs/novaseal-key-management.md), never alongside "
            "ordinary data backups.[/yellow]"
        )
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
    except (BackupVerifyError, OSError) as exc:
        # OSError is caught deliberately: pointing --set at a directory or an
        # unreadable file is a routine operator mistake, and `verify_backup`
        # does not currently wrap those into BackupVerifyError, so they used
        # to surface as a raw traceback with exit 0-by-accident instead of a
        # clean failure. A verification command must fail legibly.
        err_console.print(f"[red]✗[/red] {exc}")
        # ADR-0192 wired source. This branch matters at least as much as the
        # `not result.ok` one below: a set that cannot even be read is a
        # backup that does not exist for recovery purposes.
        from novafabric.events.sources import emit_backup_failed_alert  # noqa: PLC0415

        emit_backup_failed_alert(
            operation="verify", target=str(set_path), reason=str(exc)
        )
        raise typer.Exit(code=1) from exc

    console.print(f"Set:     {result.set_id} ({result.profile})")
    console.print(f"Members: {len(result.ok_members)} ok / {len(result.checks)} total")
    if result.includes_keys:
        console.print(
            "[yellow]⚠ This set contains KEY MATERIAL (created with "
            "--include-keys) — handle with key-custody care.[/yellow]"
        )

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
        # ADR-0192 wired source: a backup you cannot verify is as good as
        # absent, and it is usually discovered at the worst moment.
        from novafabric.events.sources import emit_backup_failed_alert  # noqa: PLC0415

        emit_backup_failed_alert(
            operation="verify",
            target=str(set_path),
            reason=(
                f"{len(result.mismatched)} mismatched, {len(result.missing)} missing, "
                f"{len(result.errors)} error(s)"
            ),
        )
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
    restore_keys: Annotated[
        bool,
        typer.Option(
            "--restore-keys",
            help="Restore key_material members from a set created with "
            "--include-keys (ADR-0216 D4). Default off: key members are "
            "skipped — key restoration requires an explicit opt-in on both "
            "the create and restore side.",
        ),
    ] = False,
    dsn: Annotated[
        Optional[str],
        typer.Option(
            "--dsn",
            help="Target Postgres DSN when restoring a pg-dump set (default: "
            "NOVA_DSN or NOVAFABRIC_POSTGRES_DSN). Never logged.",
            show_default=False,
        ),
    ] = None,
    backend: Annotated[
        Optional[str],
        typer.Option(
            "--backend",
            help="Object-store backend when restoring a manifest-only set: "
            "local | s3 | minio | ceph_rgw | azure_blob.",
            envvar="NOVA_OCS_BACKEND",
            show_default=False,
        ),
    ] = None,
    sample: Annotated[
        int,
        typer.Option(
            "--sample",
            help="Manifest-only sets: spot-check this many capsule payload "
            "hashes against the live bucket (0 = off).",
        ),
    ] = 0,
) -> None:
    """Restore a local-profile backup set, then run the verification chain.

    Normative order (ADR-0181 / backup-restore-v0 spec): verify the set →
    prepare the home → extract → migrate to head → replay crypto-shreds
    (shredded data stays shredded) → doctor storage check + seal log verify.
    The restore is complete ONLY when verification passes — there is no flag
    to skip it. Exit 1 on any failed step.

    pg-dump sets restore automatically (ADR-0217): a non-empty target DB is
    refused without --force (which first takes a safety dump), pg_restore
    runs in a single transaction (failure leaves the DB unchanged), then
    alembic migrations, manifest-anchored row counts, and RLS enforcement
    are verified. The DSN is never logged.

    \b
    Examples:
      nova restore nova-backup-01J….tar.gz
      nova restore set.tar.gz --home /srv/novafabric --force
      nova restore pg-nightly.tar.gz --dsn postgresql://…
    """
    from novafabric._paths import nova_home
    from novafabric.backup.restore import RestoreError, restore_backup
    from novafabric.backup.restore_manifest import BucketUnreachableError

    resolved_home = home or nova_home()

    try:
        result = restore_backup(
            set_path,
            home=resolved_home,
            force=force,
            restore_keys=restore_keys,
            dsn=dsn,
            backend=backend,
            sample=sample,
        )
    except RestoreError as exc:
        err_console.print(f"[red]✗[/red] {exc}")
        from novafabric.events.sources import emit_backup_failed_alert  # noqa: PLC0415

        emit_backup_failed_alert(
            operation="restore", target=str(set_path), reason=str(exc)
        )
        # Exit 2 = the listed bucket is unreachable (manifest-only sets) —
        # distinct from a failed restore so operators can branch on it.
        raise typer.Exit(
            code=2 if isinstance(exc, BucketUnreachableError) else 1
        ) from exc

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
