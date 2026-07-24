# src/novafabric/cli/lineage_export_graph.py
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from novafabric.lineage._store import LineageGraphTooLargeError, LineageStore
from novafabric.lineage.analytics._graph import collect_subgraph
from novafabric.lineage.analytics.export_interop import (
    to_cypher,
    to_gexf,
    to_graphml,
)

console = Console()


class GraphExportFormat(str, Enum):
    graphml = "graphml"
    gexf = "gexf"
    cypher = "cypher"


_RENDERERS = {
    GraphExportFormat.graphml: to_graphml,
    GraphExportFormat.gexf: to_gexf,
    GraphExportFormat.cypher: to_cypher,
}


def export_graph_cmd(
    fmt: Annotated[
        GraphExportFormat,
        typer.Option(
            "--format", "-f",
            help="Export format: graphml (Gephi/yEd), gexf (Gephi), "
            "or cypher (Neo4j MERGE statements).",
        ),
    ] = GraphExportFormat.graphml,
    out: Annotated[
        Optional[Path],
        typer.Option("--out", "-o", help="Output file (default: stdout)."),
    ] = None,
    ref: Annotated[
        Optional[str],
        typer.Option(
            "--ref",
            help="Export only the neighbourhood (provenance + blast radius) "
            "of this node instead of the whole graph.",
        ),
    ] = None,
    kind: Annotated[
        Optional[str],
        typer.Option("--kind", help="Node kind for --ref: run|asset|artifact"),
    ] = None,
    depth: Annotated[
        int, typer.Option("--depth", "-d", help="Neighbourhood depth for --ref.")
    ] = 5,
) -> None:
    """Export the lineage graph for enterprise graph tooling.

    Experimental (ADR-0214). Byte-stable GraphML/GEXF/Cypher over the local
    lineage graph, whole-graph by default or a node neighbourhood via --ref.
    Topology and attributes only — seal/signature material never travels with
    this export.

    Scope: lineage graph (local store).

    \b
    Examples:
      # Whole graph as GraphML on stdout
      nova lineage export-graph

      # Neo4j-ready Cypher to a file
      nova lineage export-graph --format cypher -o lineage.cypher

      # Just the neighbourhood of one asset
      nova lineage export-graph --ref my-model@v1 --kind asset --depth 3
    """
    store = LineageStore()
    try:
        if ref is not None:
            nodes, edges = collect_subgraph(store, ref, kind=kind, depth=depth)
            if not nodes:
                console.print(f"[red]x[/red] Unknown ref: {ref}")
                raise typer.Exit(code=1)
        else:
            nodes, edges = store.all_nodes(), store.all_edges()
    except LineageGraphTooLargeError as exc:
        console.print(f"[red]x[/red] {exc}")
        raise typer.Exit(code=1) from exc

    rendered = _RENDERERS[fmt](nodes, edges)
    if out is None:
        typer.echo(rendered, nl=False)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    console.print(
        f"[green]✓[/green] {fmt.value} export written to {out} "
        f"({len(nodes)} nodes, {len(edges)} edges)"
    )
