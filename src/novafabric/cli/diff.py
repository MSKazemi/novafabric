from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.registry.service import AssetNotFoundError, get_asset


class DiffOutputFormat(str, Enum):
    text = "text"
    json = "json"
    github_annotation = "github-annotation"

console = Console()


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten(v, key))
        else:
            result[key] = v
    return result


def _capsule_diff(
    capsule_a: Path,
    capsule_b: Path,
    output_format: DiffOutputFormat,
    assert_no_regressions: bool,
) -> None:
    from novafabric.diff._engine import DiffEngine
    from novafabric.diff._format import format_github_annotations, format_json, format_text

    for p in (capsule_a, capsule_b):
        if not p.is_dir() or not (p / "capsule.yaml").exists():
            console.print(f"[red]Not a valid capsule directory: {p}[/red]")
            raise typer.Exit(code=1)

    report = DiffEngine().compare(capsule_a, capsule_b)

    if output_format == "json":
        console.print(format_json(report))
    elif output_format == "github-annotation":
        console.print(format_github_annotations(report))
    else:
        console.print(format_text(report))

    if assert_no_regressions and report.changed_count > 0:
        raise typer.Exit(code=1)


def diff_cmd(
    ref_a: Annotated[str, typer.Argument(help="name@version  or  path/to/capsule-a")],
    ref_b: Annotated[str, typer.Argument(help="name@version  or  path/to/capsule-b")],
    output_format: Annotated[
        DiffOutputFormat,
        typer.Option("--output-format", help="Output format.")
    ] = DiffOutputFormat.text,
    assert_no_regressions: Annotated[
        bool, typer.Option("--assert-no-regressions", help="Exit 1 if any changes detected")
    ] = False,
) -> None:
    """Compare two asset versions or two run capsules.

    Accepts either asset refs (name@version) or capsule directory paths.
    Both arguments must be the same type — two asset refs or two capsule paths.

    Scope: two assets or two capsules.

    \b
    Examples:
      # Compare two capsule directories
      nova diff runs/run-01/ runs/run-02/

      # Compare two registered asset versions
      nova diff my-agent@v1.0 my-agent@v1.1

      # Output GitHub annotation format (for CI)
      nova diff --output-format github-annotation runs/run-01/ runs/run-02/

      # Fail CI if any difference is found
      nova diff --assert-no-regressions my-agent@v1.0 my-agent@v1.1
    """
    # Route to capsule diff if neither arg looks like an asset ref (name@version)
    if "@" not in ref_a or "@" not in ref_b:
        _capsule_diff(
            Path(ref_a), Path(ref_b), output_format, assert_no_regressions
        )
        return

    def parse_ref(ref: str) -> tuple[str, str]:
        n, v = ref.rsplit("@", 1)
        return n, v

    name_a, version_a = parse_ref(ref_a)
    name_b, version_b = parse_ref(ref_b)

    try:
        asset_a = get_asset(name_a, version_a)
        asset_b = get_asset(name_b, version_b)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    spec_a = _flatten(json.loads(asset_a.get("spec_json", "{}")))
    spec_b = _flatten(json.loads(asset_b.get("spec_json", "{}")))

    all_keys = set(spec_a) | set(spec_b)
    diffs = {
        k: (spec_a.get(k), spec_b.get(k))
        for k in sorted(all_keys)
        if spec_a.get(k) != spec_b.get(k)
    }

    if not diffs:
        console.print("[green]No differences found.[/green]")
        return

    console.print(f"--- {ref_a}")
    console.print(f"+++ {ref_b}")
    console.print()
    for k, (va, vb) in diffs.items():
        console.print(f"  [cyan]{k}[/cyan]: {va!r} → {vb!r}")
