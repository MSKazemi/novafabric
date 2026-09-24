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
"""``nova dashboard`` — widgets and dashboards as portable files (ADR-0235 D3).

The CLI is the interface, not an afterthought: `non-goals.md` makes the CLI the
source of truth, and files-plus-CLI is what turns a dashboard into a reviewable
artifact in `git` rather than clicked-together state living in one browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from novafabric.dashboards import (
    DashboardError,
    apply_path,
    default_store,
    load_dashboard,
    load_widget,
)

console = Console()

dashboard_app = typer.Typer(
    help="Manage dashboards and widgets as portable JSON files (experimental, ADR-0235).",
    no_args_is_help=True,
)


@dashboard_app.command("list")
def list_cmd() -> None:
    """List the widgets and dashboards on disk.

    \b
    Examples:
      nova dashboard list
    """
    store = default_store()
    try:
        widgets = store.list_widgets()
        dashboards = store.list_dashboards()
    except DashboardError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if not widgets and not dashboards:
        console.print(
            f"No dashboards or widgets in {store.root}.\n"
            "Create one with `nova dashboard apply <file.widget.json>`."
        )
        return

    if dashboards:
        table = Table(title="Dashboards", show_lines=False)
        table.add_column("id")
        table.add_column("title")
        table.add_column("widgets", justify="right")
        table.add_column("missing", justify="right")
        for dash in dashboards:
            missing = store.unresolved_widget_ids(dash)
            table.add_row(
                dash.id,
                dash.title,
                str(len(dash.widget_ids)),
                f"[red]{len(missing)}[/red]" if missing else "0",
            )
        console.print(table)

    if widgets:
        table = Table(title="Widgets")
        table.add_column("id")
        table.add_column("title")
        table.add_column("chart")
        for widget in widgets:
            table.add_row(widget.id, widget.title, widget.chart)
        console.print(table)


@dashboard_app.command("show")
def show_cmd(
    identifier: Annotated[str, typer.Argument(help="Widget or dashboard id.")],
) -> None:
    """Print one widget or dashboard as JSON.

    \b
    Examples:
      nova dashboard show run-throughput
    """
    store = default_store()
    for getter in (store.get_widget, store.get_dashboard):
        try:
            console.print_json(getter(identifier).to_json())
            return
        except DashboardError:
            continue
    console.print(f"[red]error:[/red] no widget or dashboard {identifier!r} in {store.root}")
    raise typer.Exit(code=1)


@dashboard_app.command("apply")
def apply_cmd(
    source: Annotated[
        Path, typer.Argument(help="A .widget.json / .dashboard.json file, or a directory.")
    ],
) -> None:
    """Install widgets and dashboards from a file or directory. Idempotent.

    Every document is validated **before anything is written**, so a directory
    is never half applied.

    \b
    Examples:
      nova dashboard apply ./my-widget.widget.json
      nova dashboard apply ./dashboards/
    """
    store = default_store()
    try:
        results = apply_path(source, store)
    except DashboardError as exc:
        console.print(f"[red]refused:[/red] {exc}")
        console.print("[dim]nothing was written[/dim]")
        raise typer.Exit(code=1) from exc

    changed = sum(1 for _, was_changed, _ in results if was_changed)
    for path, was_changed, kind in results:
        state = "[green]written[/green]" if was_changed else "[dim]unchanged[/dim]"
        console.print(f"{state} {kind} {path.name}")
    console.print(f"\n{len(results)} document(s), {changed} changed.")


@dashboard_app.command("export")
def export_cmd(
    identifier: Annotated[str, typer.Argument(help="Widget or dashboard id.")],
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write here instead of stdout.")
    ] = None,
) -> None:
    """Write one widget or dashboard out verbatim.

    Fields this build does not recognise are preserved (ADR-0235 D6), so
    exporting a file authored by a newer version does not silently drop its
    settings.

    \b
    Examples:
      nova dashboard export run-throughput -o ./run-throughput.widget.json
    """
    store = default_store()
    for getter, suffix in (
        (store.get_widget, ".widget.json"),
        (store.get_dashboard, ".dashboard.json"),
    ):
        try:
            payload = getter(identifier).to_json()
        except DashboardError:
            continue
        if out is None:
            typer.echo(payload, nl=False)
        else:
            target = out if out.suffix else out / f"{identifier}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
            console.print(f"[green]wrote[/green] {target}")
        return
    console.print(f"[red]error:[/red] no widget or dashboard {identifier!r} in {store.root}")
    raise typer.Exit(code=1)


@dashboard_app.command("validate")
def validate_cmd(
    source: Annotated[Path, typer.Argument(help="A file or directory to check.")],
) -> None:
    """Validate files without installing them.

    Checks the JSON Schema first, then hands any embedded query to the ADR-0129
    DSL — so a widget cannot express a query `nova query` would itself refuse.

    \b
    Examples:
      nova dashboard validate ./untrusted.widget.json
    """
    paths: list[Path]
    if source.is_dir():
        paths = sorted(
            p for p in source.iterdir() if p.name.endswith((".widget.json", ".dashboard.json"))
        )
    elif source.is_file():
        paths = [source]
    else:
        console.print(f"[red]error:[/red] {source} does not exist")
        raise typer.Exit(code=1)

    failures = 0
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(document, dict) and document.get("$novafabricDashboard"):
                load_dashboard(document)
                kind = "dashboard"
            else:
                load_widget(document)
                kind = "widget"
        except (DashboardError, json.JSONDecodeError) as exc:
            failures += 1
            console.print(f"[red]invalid[/red] {path.name}: {exc}")
        else:
            console.print(f"[green]valid[/green]   {path.name} ({kind})")

    if failures:
        console.print(f"\n{failures} of {len(paths)} file(s) invalid.")
        raise typer.Exit(code=1)
    console.print(f"\n{len(paths)} file(s) valid.")
