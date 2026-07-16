"""nova merkle-tree — render a sealed capsule's Merkle proof tree.

ADR-0172 (data slice), read-only. Loads a JSON document with a capsule's leaf hashes (and
optionally field-path labels, the sealed root, and the RFC 3161 TSR hash) and renders the proof
tree: ``leaf → intermediate → seal-root → tsr``. This is the CLI/JSON half of feature F-04; the
`web/` interactive proof tree is future design.

The tree is derived from NovaSeal's canonical layer enumerator, so the recomputed root matches the
sealed root byte-for-byte. Full leaf hashes are never printed — only short prefixes (ADR-0009);
leaf labels are field paths, never values.

Exit codes: 0 — rendered (sealed+verified, or unsealed); 1 — a seal-root mismatch (tamper);
2 — the input is missing or malformed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

_MARK = {"verified": "[green]✓[/green]", "mismatch": "[red]✗[/red]", "unverified": "[dim]○[/dim]"}


def merkle_tree_cmd(
    document: Annotated[
        Path,
        typer.Argument(help="JSON with 'leaf_hashes' (+ optional labels/sealed_root/tsr_hash)."),
    ],
    capsule_id: Annotated[
        str | None,
        typer.Option("--capsule-id", help="Capsule id to label the tree with."),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the proof tree as JSON."),
    ] = False,
) -> None:
    """Render an Evidence Provenance Merkle proof tree from a sealed capsule's hashes.

    \b
    Examples:
      nova merkle-tree tree.json
      nova merkle-tree tree.json --json --capsule-id run-42
    """
    from novafabric.trust.merkle_view import NodeType, VerifyState, build_proof_tree

    if not document.exists():
        err_console.print(f"[red]Merkle document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or "leaf_hashes" not in doc:
        err_console.print("[red]Document must be a JSON object with 'leaf_hashes'.[/red]")
        raise typer.Exit(2)

    try:
        tree = build_proof_tree(
            list(doc["leaf_hashes"]),
            leaf_labels=doc.get("leaf_labels"),
            sealed_root=doc.get("sealed_root"),
            tsr_hash=doc.get("tsr_hash"),
            capsule_id=capsule_id,
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid Merkle document:[/red] {exc}")
        raise typer.Exit(2) from exc

    mismatch = any(n.verify_state is VerifyState.mismatch for n in tree.nodes)

    if json_out:
        print(json.dumps(tree.model_dump(mode="json"), indent=2))
        raise typer.Exit(1 if mismatch else 0)

    if mismatch:
        state = "[red]MISMATCH[/red]"
    elif tree.sealed:
        state = "sealed"
    else:
        state = "[dim]unsealed[/dim]"
    header = f"Merkle proof tree: {state}"
    if tree.capsule_id:
        header += f"   capsule: {tree.capsule_id}"
    console.print(header)
    for node in tree.nodes:
        indent = "  " * (node.layer + 1)
        mark = _MARK.get(node.verify_state.value, "○")
        label = f"  {node.label}" if node.label and node.node_type is NodeType.leaf else ""
        # node_type is rendered as plain text (no square brackets — rich would treat
        # "[seal-root]" as markup and strip it).
        console.print(
            f"{indent}{mark} {node.node_type.value:<12} {node.hash_prefix} "
            f"({node.verify_state.value}){label}"
        )

    raise typer.Exit(1 if mismatch else 0)
