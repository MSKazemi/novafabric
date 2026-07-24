"""`nova search` — full-text search over redacted capsule content (ADR-0204).

Experimental. Local-first: reads the registry DB and capsule directory
directly — no server, no network. The index only ever contains
post-redaction capsule text (the corpus is a strict subset of the secret
scanner's targets), and user query input is always quoted before it reaches
the FTS5 MATCH grammar — operators like ``OR``/``NEAR``/``-`` match
literally.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from novafabric._paths import default_capsule_dir, registry_db_path
from novafabric.query.content_index import (
    SNIPPET_MARKERS,
    ContentIndexUnavailableError,
    ContentSearchError,
    EmptyQueryError,
    RunContentHits,
    reindex,
    search_content,
)

_STREAMS = "model-call-messages|tool-call-arguments|trace|capsule-yaml"


def _hits_to_items(hits: list[RunContentHits]) -> list[dict[str, object]]:
    return [
        {
            "run_id": h.run_id,
            "created_at": h.created_at,
            "status": h.status,
            "matches": [
                {
                    "stream": m.stream,
                    "ref": m.ref,
                    "line_no": m.line_no,
                    "snippet": m.snippet,
                }
                for m in h.matches
            ],
            "matches_truncated": h.matches_truncated,
        }
        for h in hits
    ]


def search_cmd(
    text: Annotated[
        Optional[str],
        typer.Argument(
            help="Search text. All terms must match (AND); a trailing '*' "
            "makes a term a prefix. FTS5 operators (OR, NEAR, -, :) are "
            "matched literally.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=200, help="Max runs returned."),
    ] = 20,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit JSON (same shape as the API items)."),
    ] = False,
    since: Annotated[
        Optional[str],
        typer.Option("--since", help="Only runs created at/after this ISO time."),
    ] = None,
    until: Annotated[
        Optional[str],
        typer.Option("--until", help="Only runs created at/before this ISO time."),
    ] = None,
    status: Annotated[
        Optional[str],
        typer.Option("--status", help="Filter by run status."),
    ] = None,
    stream: Annotated[
        Optional[str],
        typer.Option("--stream", help=f"Restrict to one stream: {_STREAMS}."),
    ] = None,
    reindex_flag: Annotated[
        bool,
        typer.Option(
            "--reindex",
            help="Backfill/repair the content index from the capsule "
            "directory instead of searching (idempotent).",
        ),
    ] = False,
    reindex_all: Annotated[
        bool,
        typer.Option(
            "--all",
            help="With --reindex: force re-extraction of every capsule, "
            "even ones already indexed.",
        ),
    ] = False,
    capsule_dir: Annotated[
        Optional[Path],
        typer.Option("--capsule-dir", help="Capsule directory (default: standard)."),
    ] = None,
    db_path: Annotated[
        Optional[Path],
        typer.Option("--db-path", help="Registry DB path (default: standard)."),
    ] = None,
) -> None:
    """Search the redacted text of local run capsules (experimental, ADR-0204)."""
    from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415

    conn = get_connection(db_path or registry_db_path())
    try:
        init_schema(conn)

        if reindex_flag:
            base = capsule_dir or default_capsule_dir()
            try:
                stats = reindex(conn, base, force=reindex_all)
            except ContentIndexUnavailableError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(code=1) from exc
            typer.echo(
                f"Reindexed {stats.indexed} capsule(s), removed {stats.removed} "
                f"stale run(s), skipped {stats.skipped_docs} doc(s)."
            )
            for err in stats.errors:
                typer.echo(f"  warning: {err}", err=True)
            return

        if text is None or not text.strip():
            typer.echo(
                "Error: provide search text, or --reindex to rebuild the index.",
                err=True,
            )
            raise typer.Exit(code=2)

        try:
            hits = search_content(
                conn,
                text,
                limit=limit,
                since=since,
                until=until,
                status=status,
                stream=stream,
            )
        except ContentIndexUnavailableError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        except EmptyQueryError as exc:
            typer.echo("Error: empty query.", err=True)
            raise typer.Exit(code=2) from exc
        except ContentSearchError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        if json_output:
            typer.echo(
                json.dumps(
                    {
                        "items": _hits_to_items(hits),
                        "snippet_markers": list(SNIPPET_MARKERS),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        if not hits:
            typer.echo("No matches.")
            return
        for h in hits:
            meta = " ".join(
                p for p in [h.created_at or "", h.status or ""] if p
            )
            typer.echo(f"{h.run_id}  {meta}".rstrip())
            for m in h.matches:
                loc = f"{m.stream}:{m.ref}" + (
                    f":{m.line_no}" if m.line_no is not None else ""
                )
                typer.echo(f"  {loc}")
                typer.echo(f"    {m.snippet}")
            if h.matches_truncated:
                typer.echo("  … more matches in this run (truncated)")
            typer.echo("")
    finally:
        conn.close()
