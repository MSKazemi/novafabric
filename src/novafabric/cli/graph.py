"""`nova graph` — execution-graph projections over captured capsules (ADR-0124).

Experimental. `nova graph agent CAPSULE_DIR` reconstructs the run's execution
DAG — model calls, tool calls, spans, and the three deterministic edge types
(`span_parent`, `agent_invokes_tool`, `follows`) — from records the capsule
already holds. Read-only, offline, content-addressed: the same capsule always
yields the same `graph_digest`. Gaps are surfaced as `reconstruction_notes`,
never silently repaired. Distinct from `nova lineage` (cross-run) and
`nova kg` (fleet security graph).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn, Optional

import typer

from novafabric.agent_graph import (
    AgentGraphError,
    build_agent_graph,
    to_dot,
    to_mermaid,
)

graph_app = typer.Typer(no_args_is_help=True)

_FORMATS = ("json", "dot", "mermaid")


def _fail(message: str, *, code: int) -> NoReturn:
    typer.echo(f"Graph error: {message}", err=True)
    raise SystemExit(code)


@graph_app.command("agent")
def agent_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Path to one captured Run Capsule directory."),
    ],
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            help="Export format: json (canonical), dot, or mermaid.",
        ),
    ] = "json",
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Write to FILE instead of stdout."),
    ] = None,
    digest: Annotated[
        bool,
        typer.Option(
            "--digest",
            help="Print only the graph_digest (for verify/diff pipelines).",
        ),
    ] = False,
    stats: Annotated[
        bool,
        typer.Option(
            "--stats",
            help="Print only the shape summary (node/edge counts, depth, fan-out).",
        ),
    ] = False,
) -> None:
    """Reconstruct one run's execution DAG from its capsule — read-only, offline.

    \b
    Examples:
      nova graph agent .novafabric/capsules/<run_id>
      nova graph agent <capsule> --digest
      nova graph agent <capsule> --format mermaid -o run-graph.mmd
    """
    if output_format not in _FORMATS:
        _fail(
            f"--format must be one of {', '.join(_FORMATS)}, got {output_format!r}",
            code=2,
        )
    if digest and stats:
        _fail("--digest and --stats are mutually exclusive", code=2)

    try:
        graph = build_agent_graph(capsule_dir)
    except AgentGraphError as exc:
        _fail(str(exc), code=1)

    if digest:
        rendered = graph.graph_digest + "\n"
    elif stats:
        stats_doc = graph.stats.to_document() if graph.stats is not None else {}
        rendered = json.dumps(stats_doc, sort_keys=True) + "\n"
    elif output_format == "dot":
        rendered = to_dot(graph)
    elif output_format == "mermaid":
        rendered = to_mermaid(graph)
    else:
        rendered = (
            json.dumps(
                graph.to_document(), indent=2, sort_keys=True, ensure_ascii=False
            )
            + "\n"
        )

    if output is not None:
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"Wrote {output}")
        return
    typer.echo(rendered, nl=False)
