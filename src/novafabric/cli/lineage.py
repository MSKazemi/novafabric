# src/novafabric/cli/lineage.py
from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.tree import Tree

from novafabric.lineage._importer import (
    ImportResult,
    import_capsule_dir,
    index_capsule_lineage,
)
from novafabric.lineage._store import LineageStore

console = Console()


class LineageOutputFormat(str, Enum):
    text = "text"
    json = "json"


lineage_app = typer.Typer(
    name="lineage",
    help="Query and manage the lineage graph for runs, assets, and artifacts.",
    no_args_is_help=True,
)

_KindArg = Annotated[
    Optional[str],
    typer.Option("--kind", help="Node kind: run|asset|artifact"),
]
_OutputArg = Annotated[
    LineageOutputFormat,
    typer.Option("--output", "-o", help="Output format."),
]
_DepthArg = Annotated[
    int,
    typer.Option("--depth", "-d", help="Max traversal depth"),
]
_EdgeTypeArg = Annotated[
    Optional[str],
    typer.Option(
        "--edge-type",
        help=(
            "Comma-separated edge types to filter: "
            "contains,spawned,delegated_to,replayed_from"
        ),
    ),
]

_VALID_EDGE_TYPES = frozenset({"contains", "spawned", "delegated_to", "replayed_from"})

_WithFacetsArg = Annotated[
    bool,
    typer.Option(
        "--with-facets",
        help="Print column-level lineage facets on traversed edges (ADR-0090)",
    ),
]


def _render_column_facets(
    store: LineageStore,
    ref: str,
    kind: str | None,
    results: list[dict[str, Any]],
) -> None:
    """Print column facets carried by edges within the traversal (ADR-0090)."""
    from novafabric.lineage._facets import COLUMN_FACET_KEY

    start = store._node_id_for_ref(ref, kind)
    node_ids = [r["node_id"] for r in results if "node_id" in r]
    if start:
        node_ids.append(start)
    faceted = [
        (edge, edge["facets"][COLUMN_FACET_KEY])
        for edge in store.edges_for_nodes(node_ids)
        if edge.get("facets", {}).get(COLUMN_FACET_KEY)
    ]
    if not faceted:
        console.print("[dim]No column facets on traversed edges[/dim]")
        return
    console.print("[bold]Column facets:[/bold]")
    for edge, facets in faceted:
        console.print(f"  edge [cyan]{edge['edge_type']}[/cyan] ({edge['edge_id']}):")
        for f in facets:
            cols = ", ".join(f.get("columns", [])) or "(row-level)"
            console.print(
                f"    [yellow]{f.get('table')}[/yellow] "
                f"{f.get('access')}/{f.get('confidence')}: {cols}"
            )


def _parse_edge_types(edge_type: str | None) -> set[str] | None:
    """Parse and validate a comma-separated edge-type filter string.

    Returns a set of validated edge type strings, or None if no filter was
    specified.  Exits with code 1 if any supplied value is unknown.
    """
    if not edge_type:
        return None
    raw = [t.strip() for t in edge_type.split(",") if t.strip()]
    invalid = [t for t in raw if t not in _VALID_EDGE_TYPES]
    if invalid:
        console.print(
            f"[red]x[/red] Unknown edge types: {', '.join(invalid)}. "
            f"Valid: {', '.join(sorted(_VALID_EDGE_TYPES))}"
        )
        raise typer.Exit(code=1)
    return set(raw)


def _get_store() -> LineageStore:
    return LineageStore()


def _render_nodes_text(nodes: list[dict[str, Any]], title: str) -> None:
    if not nodes:
        console.print(f"[dim]No results for {title}[/dim]")
        return
    tree = Tree(f"[bold]{title}[/bold]")
    for n in nodes:
        tree.add(f"[cyan]{n['kind']}[/cyan]:[yellow]{n['ref']}[/yellow]")
    console.print(tree)


@lineage_app.command("provenance")
def provenance_cmd(
    ref: Annotated[str, typer.Argument(help="Node ref (run_id, asset ref, etc.)")],
    depth: _DepthArg = 5,
    kind: _KindArg = None,
    output: _OutputArg = LineageOutputFormat.text,
    edge_type: _EdgeTypeArg = None,
    with_facets: _WithFacetsArg = False,
) -> None:
    """Show the upstream provenance chain for a run, asset, or artifact.

    Traces backwards through the lineage graph to show what produced
    the given node.

    Scope: lineage graph.

    \b
    Examples:
      # Show provenance of a run
      nova lineage provenance 01HX...

      # JSON output for scripting
      nova lineage provenance 01HX... --output json

      # Filter to spawned edges only
      nova lineage provenance 01HX... --edge-type spawned
    """
    filter_types = _parse_edge_types(edge_type)
    store = _get_store()
    results = store.provenance(ref=ref, kind=kind, depth=depth)
    if filter_types is not None:
        results = [r for r in results if r.get("edge_type") in filter_types]
    if not results and not store._node_id_for_ref(ref, kind):
        console.print(f"[red]x[/red] Unknown ref: {ref}")
        raise typer.Exit(code=1)
    if output == "json":
        typer.echo(json.dumps(results, indent=2))
    else:
        _render_nodes_text(results, f"Provenance of {ref}")
    if with_facets:
        _render_column_facets(store, ref, kind, results)


@lineage_app.command("blast-radius")
def blast_radius_cmd(
    ref: Annotated[str, typer.Argument(help="Node ref (run_id, asset ref, etc.)")],
    depth: _DepthArg = 5,
    kind: _KindArg = None,
    output: _OutputArg = LineageOutputFormat.text,
    edge_type: _EdgeTypeArg = None,
    with_facets: _WithFacetsArg = False,
) -> None:
    """Show all downstream nodes that depend on a run or asset.

    Traces forward through the lineage graph. Useful for impact analysis:
    which runs will be affected if this asset is retracted or rolled back?

    Scope: lineage graph.

    \b
    Examples:
      # Blast radius of an asset version
      nova lineage blast-radius my-agent@v1.0

      # Blast radius of a specific run
      nova lineage blast-radius 01HX...

      # Limit traversal depth
      nova lineage blast-radius my-agent@v1.0 --depth 3
    """
    filter_types = _parse_edge_types(edge_type)
    store = _get_store()
    results = store.blast_radius(ref=ref, kind=kind, depth=depth)
    if filter_types is not None:
        results = [r for r in results if r.get("edge_type") in filter_types]
    if not results and not store._node_id_for_ref(ref, kind):
        console.print(f"[red]x[/red] Unknown ref: {ref}")
        raise typer.Exit(code=1)
    if output == "json":
        typer.echo(json.dumps(results, indent=2))
    else:
        _render_nodes_text(results, f"Blast radius of {ref}")
    if with_facets:
        _render_column_facets(store, ref, kind, results)


@lineage_app.command("replay-chain")
def replay_chain_cmd(
    run_id: Annotated[str, typer.Argument(help="Run ID to trace replay chain from")],
    output: _OutputArg = LineageOutputFormat.text,
) -> None:
    """Trace the replay ancestry of a run from its run ID.

    Shows the sequence of replays that produced this run, ordered by
    step number.

    Scope: lineage graph.

    \b
    Examples:
      nova lineage replay-chain 01HX...
      nova lineage replay-chain 01HX... --output json
    """
    store = _get_store()
    results = store.replay_chain(run_id=run_id)
    if output == "json":
        typer.echo(json.dumps(results, indent=2))
    else:
        if not results:
            console.print(f"[dim]No replay chain found for {run_id}[/dim]")
        else:
            tree = Tree(f"[bold]Replay chain from {run_id}[/bold]")
            for n in results:
                step = n.get("step", "?")
                label = (
                    f"[dim]step {step}[/dim] "
                    f"[cyan]{n['kind']}[/cyan]:[yellow]{n['ref']}[/yellow]"
                )
                tree.add(label)
            console.print(tree)


@lineage_app.command("time-travel")
def time_travel_cmd(
    ref: Annotated[str, typer.Argument(help="Node ref")],
    asof: Annotated[
        str, typer.Option("--asof", help="ISO-8601 UTC timestamp snapshot point")
    ],
    kind: _KindArg = None,
    output: _OutputArg = LineageOutputFormat.text,
) -> None:
    """Show lineage edges as they existed at a specific UTC timestamp.

    Reconstructs the lineage graph state at a past point in time.
    Use ISO-8601 format for --asof (e.g. 2026-05-01T12:00:00Z).

    Scope: lineage graph.

    \b
    Examples:
      # Show lineage snapshot as of 2026-05-01
      nova lineage time-travel my-agent@v1.0 --asof 2026-05-01T12:00:00Z

      # JSON output
      nova lineage time-travel my-agent@v1.0 --asof 2026-05-01T12:00:00Z --output json
    """
    store = _get_store()
    results = store.time_travel(ref=ref, asof=asof, kind=kind)
    if output == "json":
        typer.echo(json.dumps(results, indent=2))
    else:
        if not results:
            console.print(f"[dim]No lineage edges for {ref} as of {asof}[/dim]")
        else:
            console.print(f"[bold]Lineage snapshot for {ref} as of {asof}:[/bold]")
            for row in results:
                console.print(
                    f"  [cyan]{row['edge_type']}[/cyan]  "
                    f"{row['source_kind']}:{row['source_ref']}  ->  "
                    f"{row['target_kind']}:{row['target_ref']}"
                    f"  [dim]{row['created_at']}[/dim]"
                )


@lineage_app.command("import")
def import_cmd(
    path: Annotated[
        Path,
        typer.Argument(help="Capsule directory or parent runs/ directory"),
    ],
) -> None:
    """Import lineage edges from one or more capsule directories.

    Accepts a single capsule directory or a parent directory containing
    multiple capsule subdirectories. Each capsule's lineage.jsonl is
    indexed into the lineage graph store.

    Scope: one or more capsules.

    \b
    Examples:
      # Import from a single capsule
      nova lineage import path/to/my-capsule/

      # Import all capsules in a directory
      nova lineage import ~/novafabric-data/capsules/
    """
    if not path.exists():
        console.print(f"[red]x[/red] Path not found: {path}")
        raise typer.Exit(code=1)

    # Single capsule dir?
    if (path / "capsule.yaml").exists():
        result = index_capsule_lineage(path)
        _print_import_result(result)
        return

    # Parent runs dir: iterate children
    results = import_capsule_dir(path)
    if not results:
        console.print("[yellow]![/yellow] No capsule directories found.")
        return
    for r in results:
        _print_import_result(r)


def _print_import_result(r: ImportResult) -> None:
    if r.skipped:
        msg = r.warning or "no lineage.jsonl"
        console.print(f"[yellow]![/yellow] skipped {r.capsule_run_id} ({msg})")
    else:
        console.print(
            f"[green]ok[/green] indexed {r.capsule_run_id} "
            f"({r.edges_indexed} edges, {r.nodes_indexed} nodes)"
        )


@lineage_app.command("export-prov")
def export_prov_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Capsule directory containing lineage.jsonl"),
    ],
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="Output file path (default: <capsule_dir>/prov.<ext>)"),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Serialization: 'prov-json' (default) or 'prov-n' (W3C PROV-N text).",
        ),
    ] = "prov-json",
) -> None:
    """Export a W3C PROV document from a capsule's lineage graph.

    Two serializations of the same provenance graph are available: PROV-JSON
    (default) and PROV-N (the human-readable notation). Scope: single capsule.

    \b
    Examples:
      # PROV-JSON to the capsule directory (default output path)
      nova lineage export-prov path/to/my-capsule/

      # PROV-N text to a specific file
      nova lineage export-prov path/to/my-capsule/ --format prov-n -o /tmp/prov.provn
    """
    fmt_norm = fmt.strip().lower()
    if fmt_norm not in {"prov-json", "prov-n"}:
        console.print(
            f"[red]x[/red] Unknown --format {fmt!r}; choose 'prov-json' or 'prov-n'."
        )
        raise typer.Exit(code=2)

    try:
        if fmt_norm == "prov-n":
            from novafabric.compliance.export.prov_n import export_prov_n

            rendered = export_prov_n(capsule_dir)
            default_name = "prov.provn"
        else:
            from novafabric.compliance.export.prov_json import export_prov_json

            rendered = json.dumps(export_prov_json(capsule_dir), indent=2)
            default_name = "prov.json"
    except FileNotFoundError as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(code=1) from exc

    out_path = Path(output) if output else capsule_dir / default_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    label = "PROV-N" if fmt_norm == "prov-n" else "PROV-JSON"
    console.print(f"[green]✓[/green] {label} written to {out_path}")


@lineage_app.command("emit-openlineage")
def emit_openlineage_cmd(
    path: Annotated[
        Path,
        typer.Argument(help="Capsule directory or parent runs/ directory"),
    ],
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="Target: - (stdout), http://..., or file path"),
    ] = None,
    with_facets: Annotated[
        bool,
        typer.Option(
            "--with-facets",
            help="NF-036: attach NovaFabric custom run facets "
            "(novafabric_capsule/eval/policy + executionParameters). Additive.",
        ),
    ] = False,
    otel_correlation: Annotated[
        bool,
        typer.Option(
            "--otel-correlation",
            help="NF-037: also attach the novafabric_otel_correlation facet "
            "(trace_id/span_id) when the capsule records them. Implies --with-facets.",
        ),
    ] = False,
) -> None:
    """Emit OpenLineage events from capsules to stdout, a file, or an HTTP endpoint.

    Scope: single capsule or a directory of capsules.

    \b
    Examples:
      # Print OpenLineage events to stdout
      nova lineage emit-openlineage path/to/my-capsule/

      # Post to a Marquez server
      nova lineage emit-openlineage path/to/my-capsule/ --output http://localhost:5000

      # Write to an NDJSON file
      nova lineage emit-openlineage path/to/my-capsule/ --output events.ndjson

      # Attach NovaFabric custom facets (capsule/eval/policy) + OTel correlation
      nova lineage emit-openlineage path/to/my-capsule/ --with-facets --otel-correlation
    """
    if not path.exists():
        console.print(f"[red]x[/red] Path not found: {path}")
        raise typer.Exit(code=1)

    # Collect capsule dirs
    if (path / "capsule.yaml").exists():
        capsule_dirs = [path]
    else:
        capsule_dirs = sorted(
            p for p in path.iterdir()
            if p.is_dir() and (p / "capsule.yaml").exists()
        )

    if not capsule_dirs:
        console.print("[yellow]![/yellow] No capsule directories found.")
        raise typer.Exit(code=0)

    # Resolve effective target
    if output is not None:
        target = output
    else:
        url_env = os.environ.get("OPENLINEAGE_URL", "")
        file_env = os.environ.get("OPENLINEAGE_FILE", "")
        target = url_env or file_env or "-"

    from novafabric.lineage._ol_transport import emit_to
    from novafabric.lineage._openlineage import build_events_from_capsule

    effective_facets = with_facets or otel_correlation
    for capsule_dir in capsule_dirs:
        events = build_events_from_capsule(
            capsule_dir,
            with_facets=effective_facets,
            with_otel_correlation=otel_correlation,
        )
        if not events:
            console.print(f"[yellow]![/yellow] skipped {capsule_dir.name} (no capsule.yaml)")
            continue
        emit_to(events, target)
        if target != "-":
            console.print(
                f"[green]✓[/green] {capsule_dir.name} ({len(events)} events → {target})"
            )
