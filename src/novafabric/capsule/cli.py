# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CLI commands for Phase 3 parent/child capsule operations.

Commands added (FR-06, FR-09, FR-13, FR-27):
  nova new-run-id              — print a fresh ULID (FR-27)
  nova validate --distributed  — validate distributed run (FR-06)
  nova show --with-children    — show parent + children tree (FR-13)
  nova lineage --edge-types    — filtered lineage traversal (FR-09)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.tree import Tree

from novafabric.capsule.ulid_util import new_ulid

console = Console()

capsule_phase3_app = typer.Typer(
    name="capsule-phase3",
    help="Phase 3 parent/child capsule operations",
    no_args_is_help=True,
)


@capsule_phase3_app.command("new-run-id")
def new_run_id_cmd() -> None:
    """Print a fresh run ID (ULID) for use as NOVAFABRIC_GLOBAL_RUN_ID.

    Set this env var before launching distributed workers so all child
    capsules are linked under one parent run.

    Scope: generates a local ID (no network, no storage).

    \b
    Examples:
      # Get a run ID
      nova new-run-id

      # Use in a script to link a distributed run
      export NOVAFABRIC_GLOBAL_RUN_ID=$(nova new-run-id)
      mpirun -n 4 python train.py
    """
    typer.echo(new_ulid())


@capsule_phase3_app.command("validate-distributed")
def validate_distributed_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Path to the parent capsule directory"),
    ],
    parent_run_id: Annotated[
        str,
        typer.Option("--parent-run-id", help="Parent run_id to validate"),
    ] = "",
) -> None:
    """Validate a distributed multi-worker run and report child capsule status.

    Checks that all expected worker capsules were created and that none
    are in an orphaned or failed state.

    Exit codes: 0=COMPLETE, 1=FAILED, 2=PARTIALLY_COMPLETE.

    Scope: single parent run (validates all child capsules).

    \b
    Examples:
      nova run validate-distributed path/to/parent-capsule/
      nova run validate-distributed path/to/capsule/ --parent-run-id 01HX...
    """
    from novafabric.capsule.validator import DistributedCapsuleValidator

    if not capsule_dir.is_dir():
        console.print(f"[red]x[/red] Not a directory: {capsule_dir}")
        raise typer.Exit(code=1)

    manifest_path = capsule_dir / "capsule.json"
    if not manifest_path.exists():
        console.print(f"[red]x[/red] capsule.json not found in {capsule_dir}")
        raise typer.Exit(code=1)

    if not parent_run_id:
        data = json.loads(manifest_path.read_text())
        parent_run_id = data.get("run_id", "unknown")

    validator = DistributedCapsuleValidator(capsule_dir)
    result = validator.validate_distributed(parent_run_id)

    status_color = {
        "COMPLETE": "green",
        "PARTIALLY_COMPLETE": "yellow",
        "FAILED": "red",
        "RUNNING": "blue",
    }.get(result.status, "white")

    console.print(
        f"[{status_color}]{result.status}[/{status_color}] {result.message}"
    )
    raise typer.Exit(code=result.exit_code)


@capsule_phase3_app.command("show")
def show_cmd(
    capsule_dir: Annotated[
        Path,
        typer.Argument(help="Path to spool directory or parent capsule directory"),
    ],
    run_id: Annotated[
        str,
        typer.Option("--run-id", help="Parent run_id to show"),
    ] = "",
    with_children: Annotated[
        bool,
        typer.Option("--with-children", help="Show child capsules in tree view"),
    ] = False,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: text|json"),
    ] = "text",
) -> None:
    """Show a parent capsule and optionally its child capsule tree.

    Scope: single parent capsule (reads from the spool directory).

    \b
    Examples:
      nova run show path/to/spool/ --run-id 01HX...
      nova run show path/to/parent-capsule/ --with-children
      nova run show path/to/parent-capsule/ --with-children --output json
    """
    from novafabric.capsule.tree_assembler import CapsuleTreeAssembler

    if not capsule_dir.is_dir():
        console.print(f"[red]x[/red] Not a directory: {capsule_dir}")
        raise typer.Exit(code=1)

    # Determine spool dir and run_id
    manifest_path = capsule_dir / "capsule.json"
    if manifest_path.exists() and not run_id:
        # capsule_dir IS the capsule dir itself
        data = json.loads(manifest_path.read_text())
        run_id = data.get("run_id", "")
        spool_dir = capsule_dir.parent
    elif run_id:
        spool_dir = capsule_dir
    else:
        console.print(
            "[red]x[/red] Specify --run-id or point to a capsule directory"
        )
        raise typer.Exit(code=1)

    assembler = CapsuleTreeAssembler(spool_dir)

    if with_children:
        tree_result = assembler.assemble_tree(run_id)
        if output == "json":
            def _node_dict(n: object) -> dict[str, Any]:
                from novafabric.capsule.tree_assembler import CapsuleNode
                assert isinstance(n, CapsuleNode)
                return {
                    "run_id": n.run_id,
                    "status": n.status,
                    "capsule_role": n.capsule_role,
                    "is_synthetic": n.is_synthetic,
                    "children": [_node_dict(c) for c in n.children],
                }
            typer.echo(json.dumps(_node_dict(tree_result.root), indent=2))
        else:
            rich_tree = Tree(
                f"[bold]{tree_result.root.run_id}[/bold] "
                f"[dim]{tree_result.root.status}[/dim]"
                + (" [yellow](synthetic)[/yellow]" if tree_result.root.is_synthetic else "")
            )

            def _add_children(t: object, node: object) -> None:
                from rich.tree import Tree as RichTree

                from novafabric.capsule.tree_assembler import CapsuleNode
                assert isinstance(node, CapsuleNode)
                assert isinstance(t, RichTree)
                for child in node.children:
                    label = (
                        f"[cyan]{child.capsule_role}[/cyan] "
                        f"{child.run_id} "
                        f"[dim]{child.status}[/dim]"
                    )
                    subtree = t.add(label)
                    _add_children(subtree, child)

            _add_children(rich_tree, tree_result.root)

            for orphan in tree_result.orphan_children:
                rich_tree.add(
                    f"[yellow]orphan[/yellow] {orphan.run_id} "
                    f"[dim]parent: unknown[/dim]"
                )

            console.print(rich_tree)
            console.print(
                f"  Total: [bold]{tree_result.total_nodes}[/bold] nodes"
            )
    else:
        children = assembler.get_children(run_id)
        if output == "json":
            typer.echo(json.dumps(children, indent=2))
        else:
            console.print(f"[bold]Children of[/bold] {run_id}:")
            if not children:
                console.print("  [dim](none)[/dim]")
            for c in children:
                console.print(
                    f"  [cyan]{c.get('capsule_role', '?')}[/cyan] "
                    f"{c.get('run_id')} "
                    f"[dim]{c.get('status')}[/dim]"
                )


@capsule_phase3_app.command("lineage")
def lineage_cmd(
    run_id: Annotated[str, typer.Argument(help="Run ID to query lineage for")],
    spool_dir: Annotated[
        Path,
        typer.Option("--spool-dir", help="Capsule spool directory"),
    ] = Path("."),
    edge_types: Annotated[
        Optional[str],
        typer.Option(
            "--edge-types",
            help=(
                "Comma-separated edge types to filter: "
                "contains,spawned,delegated_to,replayed_from"
            ),
        ),
    ] = None,
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output format: text|json"),
    ] = "text",
) -> None:
    """Show lineage edges for a distributed run, with optional edge-type filtering.

    Scope: single parent run (reads lineage.jsonl from the spool directory).

    \b
    Examples:
      nova run lineage 01HX... --spool-dir path/to/spool/
      nova run lineage 01HX... --edge-types delegated_to
      nova run lineage 01HX... --output json
    """
    from novafabric.capsule.schema import EdgeType

    # Parse edge type filter
    filter_types: set[str] | None = None
    if edge_types:
        raw_types = [t.strip() for t in edge_types.split(",") if t.strip()]
        valid_values = {e.value for e in EdgeType}
        invalid = [t for t in raw_types if t not in valid_values]
        if invalid:
            console.print(
                f"[red]x[/red] Unknown edge types: {', '.join(invalid)}. "
                f"Valid: {', '.join(sorted(valid_values))}"
            )
            raise typer.Exit(code=1)
        filter_types = set(raw_types)

    # Scan spool for lineage edges involving this run_id
    edges: list[dict[str, Any]] = []
    if spool_dir.is_dir():
        for entry in spool_dir.iterdir():
            if not entry.is_dir():
                continue
            lineage_path = entry / "lineage.jsonl"
            if not lineage_path.exists():
                continue
            for line in lineage_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                src = record.get("source_run_id", "")
                tgt = record.get("target_run_id", "")
                if run_id not in (src, tgt):
                    continue
                et = record.get("edge_type", "contains")
                if filter_types and et not in filter_types:
                    continue
                edges.append(record)

    if output == "json":
        typer.echo(json.dumps(edges, indent=2))
    else:
        if not edges:
            console.print(
                f"[dim]No lineage edges found for {run_id}"
                + (f" (filter: {edge_types})" if edge_types else "")
                + "[/dim]"
            )
            return
        console.print(
            f"[bold]Lineage edges for[/bold] {run_id}"
            + (f" [dim](filter: {edge_types})[/dim]" if edge_types else "") + ":"
        )
        for e in edges:
            src = e.get("source_run_id", "?")
            tgt = e.get("target_run_id", "?")
            et = e.get("edge_type", "?")
            console.print(
                f"  [cyan]{et}[/cyan]  {src} → {tgt}"
            )
