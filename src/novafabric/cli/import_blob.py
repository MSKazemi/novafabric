"""``nova import`` — verified batch import of an ADR-0141 blob export.

Experimental (ADR-0207 P1). The inverse of ``nova export-blob``: verifies the
signed export manifest first (fail-closed), unpacks members through a hardened
staging directory, reindexes lineage + the dashboard runs cache, and leaves a
JSON import receipt under ``$NOVAFABRIC_HOME/import-receipts/``.

Named ``import`` at the CLI; the module is ``import_blob`` because ``import``
is a Python reserved word (mirrors ``export_blob``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

import typer
from rich.console import Console

if TYPE_CHECKING:
    from novafabric.import_blob.service import ImportOutcome

console = Console()
err_console = Console(stderr=True)


def import_cmd(
    source: Annotated[
        Path,
        typer.Argument(
            help=(
                "An export layout: a directory containing export-manifest.json "
                "and objects/, or a single .tar/.tar.gz archive of that layout."
            ),
        ),
    ],
    public_key: Annotated[
        Optional[Path],
        typer.Option(
            "--public-key",
            help=(
                "PEM-encoded ed25519 public key of the export signer "
                "(from `nova export-blob --public-key-out`). Required unless "
                "--allow-unsigned is passed."
            ),
        ),
    ] = None,
    allow_unsigned: Annotated[
        bool,
        typer.Option(
            "--allow-unsigned",
            help=(
                "Skip the DSSE SIGNATURE check ONLY — authorship goes "
                "unverified, loudly. All content-hash, size, and consistency "
                "checks still run and still refuse on mismatch. Recorded "
                "permanently in the receipt and audit log."
            ),
        ),
    ] = False,
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help="Target capsule store root (default: $NOVAFABRIC_HOME/capsules).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Verify and classify every member (would-import / would-skip / "
                "collision) with zero writes to the store or indexes. Same "
                "exit codes as a wet run, so DR drills can gate on it."
            ),
        ),
    ] = False,
    no_reindex: Annotated[
        bool,
        typer.Option(
            "--no-reindex",
            help="Unpack only; skip lineage and runs-cache updates.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the import report as JSON to stdout."),
    ] = False,
) -> None:
    """Import a verified batch export into the local capsule store.

    Verification-first and fail-closed: nothing enters the store unless the
    signed manifest verifies and every member's bytes re-hash correctly.
    Idempotent — re-running an import of an unchanged batch is a no-op. A
    capsule that exists locally with different content is a collision and is
    NEVER overwritten (delete it with existing retention tooling and re-import
    to replace it). Every run — refusals and dry runs included — writes an
    import receipt and one audit entry.

    \b
    Exit codes (spec batch-import-v0):
      0  success (all imported / skipped as already present)
      2  usage error (bad flags, unreadable source)
      3  verification refused: INVALID (structure, signature, digest)
      4  verification refused: INCOMPLETE (member missing / hash mismatch)
      5  collision(s): same run_id, different content
      6  member(s) failed during unpack

    \b
    Examples:
      # Verified import (the normal path)
      nova import ./exports/audit-q3 --public-key export.pub.pem

      # DR drill: prove restorability without writing anything
      nova import ./exports/audit-q3 --public-key export.pub.pem --dry-run

      # Air-gap courier archive
      nova import batch.tar.gz --public-key export.pub.pem
    """
    import json as json_mod

    from novafabric.import_blob.service import (
        EXIT_USAGE,
        ImportUsageError,
        import_batch,
    )

    pem: bytes | None = None
    if public_key is not None:
        try:
            pem = public_key.read_bytes()
        except OSError as exc:
            err_console.print(f"[red]Cannot read --public-key:[/red] {exc}")
            raise typer.Exit(code=EXIT_USAGE)

    if allow_unsigned:
        err_console.print(
            "[yellow]WARNING:[/yellow] --allow-unsigned: the manifest's DSSE "
            "signature will NOT be checked — authorship is unverified. Content "
            "hashes and batch consistency are still enforced."
        )

    try:
        outcome = import_batch(
            source,
            capsule_root=capsule_dir,
            public_key_pem=pem,
            allow_unsigned=allow_unsigned,
            dry_run=dry_run,
            reindex=not no_reindex,
        )
    except ImportUsageError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=EXIT_USAGE)

    receipt = outcome.receipt
    if json_output:
        # Plain print: machine-parseable, never soft-wrapped by the console.
        print(json_mod.dumps(receipt.to_json_dict(), indent=2))
    else:
        _print_summary(outcome)

    raise typer.Exit(code=outcome.exit_code)


def _print_summary(outcome: ImportOutcome) -> None:
    receipt = outcome.receipt
    counts = receipt.counts
    verification = receipt.verification

    if verification.status != "VALID":
        err_console.print(
            f"[red]Import refused:[/red] verification {verification.status} — "
            "nothing imported"
        )
        for problem in verification.problems:
            err_console.print(f"  [red]•[/red] {problem}")
    else:
        label = "[cyan]DRY RUN[/cyan] — would import" if receipt.dry_run else "Imported"
        console.print(
            f"[green]✓[/green] {label} {counts.imported} capsule(s), "
            f"{counts.skipped_existing} already present, "
            f"{counts.collisions} collision(s), {counts.failed} failed"
        )
        for member in receipt.members:
            if member.action == "collision":
                console.print(
                    f"  [red]collision:[/red] {member.capsule_id} — local capsule "
                    "differs from the batch (never overwritten; see receipt for "
                    "both hashes)"
                )
            elif member.action == "failed":
                console.print(
                    f"  [red]failed:[/red] {member.capsule_id} — {member.detail}"
                )
        if receipt.reindex.errors:
            for error in receipt.reindex.errors:
                err_console.print(f"  [yellow]reindex:[/yellow] {error}")

    console.print(f"  import_id:    {receipt.import_id}")
    if receipt.export_id:
        console.print(f"  export_id:    {receipt.export_id}")
    console.print(f"  verification: {verification.mode} / {verification.status}")
    if outcome.receipt_path is not None:
        console.print(f"  receipt:      {outcome.receipt_path}")
