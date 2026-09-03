# src/novafabric/cli/memory.py
"""`nova memory` — memory provenance queries (ADR-0143 P1, NF-111/NF-114)."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from novafabric.capture.events import MemoryOperationEvent
from novafabric.lineage.memory import edges_for_events, readers_of, writers_of

console = Console()
err_console = Console(stderr=True)

MEMORY_EVENTS_FILE = "memory_operations.jsonl"


class MemoryOutputFormat(str, Enum):
    text = "text"
    json = "json"


memory_app = typer.Typer(
    name="memory",
    help="Query memory provenance: who wrote a memory item, and who read it.",
    no_args_is_help=True,
)

_CapsuleArg = Annotated[Path, typer.Argument(help="Path to a capsule directory.")]
_OutputArg = Annotated[
    MemoryOutputFormat, typer.Option("--output", "-o", help="Output format.")
]
_KeyArg = Annotated[str, typer.Option("--key", "-k", help="Memory key to trace.")]
# NOTE: no --namespace flag. `novafabric.lineage.memory` can namespace memory
# node refs, but a CLI flag would be applied to both edge construction and the
# subsequent query, so it would cancel out and could never change the answer.
# Namespacing belongs to whoever records the events, not to the reader.


def _load_events(capsule: Path) -> list[MemoryOperationEvent]:
    """Read memory operations from a capsule.

    A capsule with no memory facet is valid (ADR-0143 P1) — it yields an empty
    list, not an error. Malformed lines are skipped rather than aborting the
    query: a partially-written tail is the normal state of a capsule from a
    crashed run, which is exactly when a back-trace is most wanted.
    """
    path = capsule / MEMORY_EVENTS_FILE
    if not path.is_file():
        return []
    events: list[MemoryOperationEvent] = []
    skipped = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            raw.pop("event_type", None)
            events.append(MemoryOperationEvent.model_validate(raw))
        except Exception:
            skipped += 1
    if skipped:
        # stderr, not stdout: a warning interleaved into `-o json` output
        # makes the payload unparseable for the caller piping it.
        err_console.print(
            f"[yellow]warning:[/yellow] skipped {skipped} unreadable line(s) in "
            f"{MEMORY_EVENTS_FILE}",
            style="dim",
        )
    return events


def _require_capsule(capsule: Path) -> None:
    if not capsule.is_dir():
        console.print(f"[red]error:[/red] not a capsule directory: {capsule}")
        raise typer.Exit(code=2)



def _gather(capsules: list[Path]) -> tuple[list[Any], dict[str, int]]:
    """Load memory edges from every capsule, with the coverage that produced them.

    Coverage travels with the answer because a blast radius is only as wide as
    the capsules searched. A reader who cannot see that 3 of 50 capsules were
    scanned will read a partial answer as a complete one — which is the failure
    this command already had at a scope of one.
    """
    edges: list[Any] = []
    with_operations = 0
    for capsule in capsules:
        _require_capsule(capsule)
        events = _load_events(capsule)
        if events:
            with_operations += 1
        edges.extend(edges_for_events(events))
    return edges, {
        "capsules_searched": len(capsules),
        "capsules_with_memory_operations": with_operations,
    }


def _dedupe(run_ids: list[str]) -> list[str]:
    """Drop repeats while preserving order.

    The same run's events can appear in more than one supplied capsule (a parent
    and its child, or the same capsule passed twice). Counting it twice would
    overstate a blast radius.
    """
    seen: set[str] = set()
    out: list[str] = []
    for run_id in run_ids:
        if run_id not in seen:
            seen.add(run_id)
            out.append(run_id)
    return out


@memory_app.command("lineage")
def memory_lineage(
    capsule: _CapsuleArg,
    output: _OutputArg = MemoryOutputFormat.text,
) -> None:
    """List the memory provenance edges implied by a capsule."""
    _require_capsule(capsule)
    edges = edges_for_events(_load_events(capsule))

    if output is MemoryOutputFormat.json:
        console.print_json(json.dumps([e.as_dict() for e in edges]))
        return

    if not edges:
        console.print(
            "No memory operations recorded in this capsule.", style="dim"
        )
        return

    table = Table(title=f"Memory lineage — {capsule.name}")
    table.add_column("edge_type")
    table.add_column("source")
    table.add_column("target")
    table.add_column("at")
    for edge in sorted(edges, key=lambda e: e.created_at):
        table.add_row(
            edge.edge_type,
            _describe(edge.source),
            _describe(edge.target),
            edge.created_at,
        )
    console.print(table)


def _describe(node: dict[str, Any]) -> str:
    if node.get("kind") == "memory":
        return f"memory:{node.get('memory_key')}"
    return f"run:{node.get('run_id')}"


@memory_app.command("trace")
def memory_trace(
    capsule: _CapsuleArg,
    key: _KeyArg,
    output: _OutputArg = MemoryOutputFormat.text,
    also: Annotated[
        list[Path] | None,
        typer.Option(
            "--also-capsule",
            help="Another capsule to search (repeatable). A blast radius spans runs.",
        ),
    ] = None,
) -> None:
    """Back-trace a memory key: which runs wrote it, and which read it.

    This is the poisoned-read query (NF-114): given a memory item implicated
    in a bad answer, `writers` is where the value came from and `readers` is
    the blast radius.

    A persistent memory store exists so one run can read what another wrote, so
    a single capsule cannot answer this: pass every capsule that might have
    touched the key with `--also-capsule`. The result always reports how many
    capsules were searched — a blast radius over 3 of 50 capsules is not the
    blast radius, and an answer whose coverage is unstated reads as complete.
    """
    capsules = [capsule, *(also or [])]
    edges, coverage = _gather(capsules)
    writers = _dedupe(writers_of(edges, key))
    readers = _dedupe(readers_of(edges, key))

    if output is MemoryOutputFormat.json:
        console.print_json(
            json.dumps(
                {
                    "memory_key": key,
                    "writers": writers,
                    "readers": readers,
                    **coverage,
                }
            )
        )
        return

    searched = coverage["capsules_searched"]
    console.print(f"[bold]memory:{key}[/bold]")
    if not writers and not readers:
        console.print(
            f"  no recorded operations on this key in the {searched} capsule(s) "
            "searched",
            style="dim",
        )
        return
    console.print(f"  written by ({len(writers)}), oldest first:")
    for run_id in writers or ["—"]:
        console.print(f"    {run_id}")
    console.print(f"  read by ({len(readers)}), oldest first:")
    for run_id in readers or ["—"]:
        console.print(f"    {run_id}")
    console.print(
        f"  searched {searched} capsule(s); "
        f"{coverage['capsules_with_memory_operations']} carried memory operations",
        style="dim",
    )
