"""``nova export-blob`` — batch capsule export with a signed completeness manifest.

Experimental (ADR-0141). Selects capsules (explicit ids/paths or a time-range
scan), writes them content-addressed to a destination via the existing storage
adapters, and emits one Ed25519/DSSE-signed ``export-manifest.json`` written
last — so a recipient can verify integrity AND completeness offline with
``nova verify export-manifest.json --public-key <pem>``.

Named ``export-blob`` (flat, like the rest of the ``export-*`` family) rather
than the ADR's ``nova export blob``: ADR-0140 shipped ``nova export --html``
as a plain command, so ``export`` cannot also be a command group. Recorded as
a deviation in ADR-0141's implementation status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def export_blob_cmd(
    dest: Annotated[
        str,
        typer.Option(
            "--dest",
            help=(
                "Destination: a local directory (always works) or s3://bucket[/prefix] "
                "(any S3-compatible endpoint; set NOVA_S3_ENDPOINT_URL for a private one). "
                "azure:// and gcs:// are planned (ADR-0141 P2)."
            ),
        ),
    ],
    capsule: Annotated[
        Optional[list[str]],
        typer.Option(
            "--capsule",
            "-c",
            help=(
                "Capsule id (under the capsule dir) or capsule path; repeatable. "
                "Mutually exclusive with --since/--until."
            ),
        ),
    ] = None,
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--capsule-dir",
            help="Capsule root to select from (default: $NOVAFABRIC_HOME/capsules).",
        ),
    ] = None,
    since: Annotated[
        Optional[str],
        typer.Option(
            "--since",
            help="Only capsules with created_at >= this RFC 3339 timestamp (inclusive).",
        ),
    ] = None,
    until: Annotated[
        Optional[str],
        typer.Option(
            "--until",
            help="Only capsules with created_at <= this RFC 3339 timestamp (inclusive).",
        ),
    ] = None,
    key: Annotated[
        Optional[Path],
        typer.Option(
            "--key",
            help=(
                "PEM-encoded ed25519 private key to sign the manifest "
                "(default: the NovaFabric keyring key, generated if absent)."
            ),
        ),
    ] = None,
    worm: Annotated[
        bool,
        typer.Option(
            "--worm",
            help=(
                "Write members and manifest under WORM retention at the destination "
                "(S3 Object Lock COMPLIANCE via the existing adapter; local dirs get "
                "best-effort read-only files). Records the intent in the manifest."
            ),
        ),
    ] = False,
    worm_retention_days: Annotated[
        int,
        typer.Option(
            "--worm-retention-days",
            help="Retention window in days for --worm (default: 365).",
        ),
    ] = 365,
    allow_unsafe_skips: Annotated[
        bool,
        typer.Option(
            "--allow-unsafe-skips",
            help=(
                "Permit export when a member's redaction-proof.json records "
                "unsafe_skips entries (mirrors nova export-evidence)."
            ),
        ),
    ] = False,
    out: Annotated[
        Optional[Path],
        typer.Option(
            "--out",
            help="Also write a local copy of export-manifest.json to this path.",
        ),
    ] = None,
    public_key_out: Annotated[
        Optional[Path],
        typer.Option(
            "--public-key-out",
            help=(
                "Write the signer's public key PEM here, for out-of-band distribution "
                "to manifest verifiers."
            ),
        ),
    ] = None,
) -> None:
    """Export a batch of capsules with a signed, verifiable completeness manifest.

    Members are written content-addressed under ``objects/<sha256>`` at the
    destination — already-present blobs are skipped, so re-runs are idempotent
    and an interrupted export resumes. The signed ``export-manifest.json`` is
    written LAST: its presence means the batch is complete. Source capsules are
    never modified.

    Scope: batch (N capsules).

    \b
    Examples:
      # Export two capsules by id to a local directory
      nova export-blob --dest ./exports/audit-q3 -c 01HX… -c 01HY…

      # Export everything captured in a time window
      nova export-blob --dest ./exports/w27 --since 2026-06-29 --until 2026-07-05

      # S3-compatible destination with WORM retention
      nova export-blob --dest s3://audit-bucket/exports/2026-07/ --worm --worm-retention-days 2555

      # Explicit signing key + save the public key for the recipient
      nova export-blob --dest ./out -c 01HX… --key ed25519.pem --public-key-out export.pub.pem
    """
    from novafabric.evidence.signing import LocalSigner
    from novafabric.export_blob.destinations import (
        DestinationError,
        resolve_destination,
        worm_retain_until,
    )
    from novafabric.export_blob.models import MANIFEST_FILENAME, WormIntent
    from novafabric.export_blob.packing import CapsulePackError
    from novafabric.export_blob.service import (
        ExportSelectionError,
        KeyringSigner,
        UnsafeSkipsExportError,
        export_batch,
        select_capsules,
    )

    try:
        selection = select_capsules(
            capsule, root=capsule_dir, since=since, until=until
        )
    except ExportSelectionError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    try:
        destination = resolve_destination(dest, worm_retention_days=worm_retention_days)
    except DestinationError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    signer: LocalSigner | KeyringSigner
    if key is not None:
        try:
            signer = LocalSigner(key)
        except (OSError, ValueError) as exc:
            err_console.print(f"[red]Failed to load signing key:[/red] {exc}")
            raise typer.Exit(code=1)
    else:
        signer = KeyringSigner()

    worm_intent = (
        WormIntent(mode="compliance", retain_until=worm_retain_until(worm_retention_days))
        if worm
        else None
    )

    try:
        result = export_batch(
            selection,
            destination,
            signer,
            worm=worm_intent,
            allow_unsafe_skips=allow_unsafe_skips,
        )
    except UnsafeSkipsExportError as exc:
        err_console.print(f"[red]Export refused:[/red] {exc}")
        raise typer.Exit(code=2)
    except (CapsulePackError, DestinationError, OSError) as exc:
        # Never blocks a user workload: report, exit non-zero, leave the
        # partial (manifest-less, resumable) state in place.
        err_console.print(f"[red]Export failed (resumable — re-run to finish):[/red] {exc}")
        raise typer.Exit(code=1)

    manifest = result.manifest
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        import json

        out.write_text(json.dumps(manifest.to_json_dict(), indent=2) + "\n")
    if public_key_out is not None:
        public_key_out.parent.mkdir(parents=True, exist_ok=True)
        public_key_out.write_bytes(signer.public_pem)

    console.print(
        f"[green]✓[/green] Exported {manifest.count} capsule(s) to {dest} "
        f"({result.written} written, {result.skipped} already present)"
    )
    console.print(f"  export_id:    {manifest.export_id}")
    console.print(f"  batch_digest: {manifest.batch_digest}")
    console.print(f"  manifest:     {dest.rstrip('/')}/{MANIFEST_FILENAME}")
    if worm_intent is not None:
        console.print(f"  worm:         compliance until {worm_intent.retain_until}")
    console.print(
        "  verify with:  nova verify <export-manifest.json> --public-key <pem>"
    )
