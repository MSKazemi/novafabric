"""nova media — read surface over content-addressed media on model calls (ADR-0125)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()

media_app = typer.Typer(no_args_is_help=True)


@media_app.command("list")
def list_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Capsule directory (contains capsule.yaml)."),
    ],
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the media parts as a JSON array."),
    ] = False,
) -> None:
    """List every content-addressed media part recorded on the capsule's model calls.

    Shows the MediaPart reference metadata (ADR-0125): type, media_type,
    sha256 content hash, byte size, whether the bytes were captured into the
    blob store (blob_ref), and the redaction flag. Integrity checking is done
    by `nova validate` (blob bytes must re-hash to the recorded content_hash).

    Scope: single capsule; read-only.

    \b
    Examples:
      # Human-readable table
      nova media list .novafabric/runs/<run-id>/

      # Machine-readable
      nova media list --json .novafabric/runs/<run-id>/
    """
    if not (capsule_dir / "capsule.yaml").exists():
        console.print(
            f"[red]✗[/red] not a capsule directory (no capsule.yaml): {capsule_dir}"
        )
        raise typer.Exit(code=2)

    from novafabric.capture.media import iter_media_parts

    rows = list(iter_media_parts(capsule_dir))

    if as_json:
        console.print_json(
            json.dumps(
                [{"model_call_id": call_id, **media} for call_id, media in rows]
            )
        )
        return

    if not rows:
        console.print("No media parts recorded in this capsule.")
        return

    table = Table(title=f"Media parts — {capsule_dir.name}")
    table.add_column("model_call_id", overflow="fold")
    table.add_column("type")
    table.add_column("media_type")
    table.add_column("content_hash", overflow="fold")
    table.add_column("bytes", justify="right")
    table.add_column("blob_ref", overflow="fold")
    table.add_column("redacted")
    for call_id, media in rows:
        table.add_row(
            call_id or "?",
            str(media.get("type", "?")),
            str(media.get("media_type", "?")),
            str(media.get("content_hash", "?")),
            str(media.get("byte_size", "?")),
            str(media.get("blob_ref") or "— (reference-only)"),
            "yes" if media.get("redacted") else "no",
        )
    console.print(table)
