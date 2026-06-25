# src/novafabric/cli/asset.py
"""``nova asset`` sub-command group — asset lifecycle operations.

Currently exposes:

    nova asset diff <name>@<v1> <name>@<v2>

        Produce a unified diff of the spec YAML/JSON for two registered
        versions of the same asset.  The diff is rendered to stdout in
        unified-diff format so it can be piped to downstream tools or
        captured in CI logs.
"""
from __future__ import annotations

import difflib
import json
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.registry.service import AssetNotFoundError, get_asset

console = Console()

asset_app = typer.Typer(
    name="asset",
    help="Asset lifecycle operations (diff, etc.)",
    no_args_is_help=True,
)


def _parse_ref(ref: str) -> tuple[str, str]:
    """Split ``name@version`` into ``(name, version)``.

    Raises :class:`typer.BadParameter` if ``@`` is absent.
    """
    if "@" not in ref:
        raise typer.BadParameter(
            f"Invalid asset ref {ref!r}: expected name@version format",
            param_hint="ref",
        )
    name, version = ref.rsplit("@", 1)
    return name, version


@asset_app.command("diff")
def asset_diff_cmd(
    ref_a: Annotated[
        str,
        typer.Argument(help="First asset ref in name@version form"),
    ],
    ref_b: Annotated[
        str,
        typer.Argument(help="Second asset ref in name@version form"),
    ],
    unified: Annotated[
        int,
        typer.Option("--unified", "-U", help="Lines of context in unified diff output"),
    ] = 3,
    output_format: Annotated[
        str,
        typer.Option(
            "--output-format",
            help="Output format: unified|json",
        ),
    ] = "unified",
) -> None:
    """Show a unified diff of the spec YAML for two registered asset versions.

    Both refs must be versions of the same asset (name@v1 vs name@v2).

    Scope: two asset versions.

    \b
    Examples:
      nova asset diff my-agent@v1.0 my-agent@v1.1
    """
    name_a, version_a = _parse_ref(ref_a)
    name_b, version_b = _parse_ref(ref_b)

    try:
        asset_a = get_asset(name_a, version_a)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    try:
        asset_b = get_asset(name_b, version_b)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    spec_a: dict[str, Any] = json.loads(asset_a.get("spec_json") or "{}")
    spec_b: dict[str, Any] = json.loads(asset_b.get("spec_json") or "{}")

    lines_a = json.dumps(spec_a, indent=2, sort_keys=True).splitlines(keepends=True)
    lines_b = json.dumps(spec_b, indent=2, sort_keys=True).splitlines(keepends=True)

    if output_format == "json":
        # Structured output: field-level changes only
        flat_a = _flatten(spec_a)
        flat_b = _flatten(spec_b)
        all_keys = set(flat_a) | set(flat_b)
        added = {k: flat_b[k] for k in all_keys if k not in flat_a}
        removed = {k: flat_a[k] for k in all_keys if k not in flat_b}
        changed = {
            k: {"from": flat_a[k], "to": flat_b[k]}
            for k in all_keys
            if k in flat_a and k in flat_b and flat_a[k] != flat_b[k]
        }
        typer.echo(
            json.dumps(
                {
                    "ref_a": ref_a,
                    "ref_b": ref_b,
                    "identical": not (added or removed or changed),
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                },
                indent=2,
            )
        )
        if added or removed or changed:
            raise typer.Exit(code=1)
        return

    # Unified diff output (default)
    diff_lines = list(
        difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=ref_a,
            tofile=ref_b,
            n=unified,
        )
    )

    if not diff_lines:
        console.print("[green]No differences found.[/green]")
        return

    for line in diff_lines:
        # Colour the diff for terminals that support it; Rich strips codes
        # when the output is not a TTY.
        stripped = line.rstrip("\n")
        if stripped.startswith("+++") or stripped.startswith("---"):
            console.print(f"[bold]{stripped}[/bold]")
        elif stripped.startswith("+"):
            console.print(f"[green]{stripped}[/green]")
        elif stripped.startswith("-"):
            console.print(f"[red]{stripped}[/red]")
        elif stripped.startswith("@@"):
            console.print(f"[cyan]{stripped}[/cyan]")
        else:
            console.print(stripped)

    raise typer.Exit(code=1)


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Recursively flatten a nested dict into dotted-path keys."""
    result: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten(v, key))
        else:
            result[key] = v
    return result
