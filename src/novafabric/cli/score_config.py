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

"""``nova eval score config`` sub-commands (ADR-0117, experimental).

The score-configuration catalog: named, immutable, content-addressed definitions of
what a valid score for a given metric ``name`` looks like.

    nova eval score config add  --name helpfulness --value-type categorical \
        --description "How helpful the turn was." \
        --category bad:0 --category ok:1 --category good:2
    nova eval score config add  --name toxicity --value-type numeric \
        --description "Lower is better." --min 0 --max 1 --direction lower-better
    nova eval score config list [--all] [--json]
    nova eval score config get  toxicity@1          # or a bare name, or sha256:<hex>
    nova eval score config show toxicity            # human view + version history

Enforcement of a config on the score-append path is opt-in via
``nova eval score add --validate-scores`` (default off — free scores stay legal).
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from novafabric.eval.score_config import (
    ScoreCategory,
    ScoreConfig,
    ScoreDirection,
    ScoreRange,
)
from novafabric.eval.score_config_catalog import (
    ScoreConfigNotFoundError,
    find_config_for_score,
    get_config,
    list_configs,
    register_config,
    resolve_config_ref,
)
from novafabric.eval.scores import ScoreValueType

console = Console()

config_app = typer.Typer(
    name="config",
    help="Manage the score-configuration catalog (immutable, content-addressed).",
    no_args_is_help=True,
)


def _parse_category(raw: str) -> ScoreCategory:
    """Parse ``value`` or ``value:ordinal`` into a :class:`ScoreCategory`."""
    if ":" in raw:
        value, _, suffix = raw.rpartition(":")
        try:
            return ScoreCategory(value=value, ordinal=int(suffix))
        except ValueError as exc:
            raise typer.BadParameter(
                f"--category {raw!r}: ordinal must be an integer (use value or value:ordinal)"
            ) from exc
    return ScoreCategory(value=raw)


def _shape_summary(config: ScoreConfig) -> str:
    if config.value_type is ScoreValueType.CATEGORICAL:
        return ", ".join(
            c.value if c.ordinal is None else f"{c.value}={c.ordinal}"
            for c in config.categories or []
        )
    if config.value_type is ScoreValueType.NUMERIC:
        rng = config.range
        assert rng is not None  # guaranteed by the model (C3)
        direction = f" ({rng.direction.value})" if rng.direction is not None else ""
        return f"[{rng.min}, {rng.max}]{direction}"
    return "true|false"


@config_app.command("add")
def config_add(
    name: Annotated[str, typer.Option(help="Metric name this config governs.")],
    value_type: Annotated[
        ScoreValueType, typer.Option("--value-type", help="boolean|categorical|numeric.")
    ],
    description: Annotated[
        str, typer.Option(help="What the metric measures (human-readable definition).")
    ],
    category: Annotated[
        list[str] | None,
        typer.Option(
            "--category",
            help="Allowed category, 'value' or 'value:ordinal' (repeatable; categorical only).",
        ),
    ] = None,
    min_: Annotated[
        float | None, typer.Option("--min", help="Range lower bound (numeric only).")
    ] = None,
    max_: Annotated[
        float | None, typer.Option("--max", help="Range upper bound (numeric only).")
    ] = None,
    direction: Annotated[
        ScoreDirection | None,
        typer.Option(help="higher-better|lower-better (numeric only, informative)."),
    ] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Free catalog tag (repeatable, advisory)."),
    ] = None,
) -> None:
    """Register a score configuration (immutable; a changed body bumps the version)."""
    categories = [_parse_category(c) for c in category] if category else None
    range_: ScoreRange | None = None
    if min_ is not None or max_ is not None or direction is not None:
        if min_ is None or max_ is None:
            raise typer.BadParameter("--min and --max must be given together")
        try:
            range_ = ScoreRange(min=min_, max=max_, direction=direction)
        except ValueError as exc:
            console.print(f"[red]Invalid range:[/red] {exc}")
            raise typer.Exit(code=1) from exc
    try:
        existing = find_config_for_score(name)
        config = register_config(
            name=name,
            value_type=value_type,
            description=description,
            categories=categories,
            range_=range_,
            labels=label,
        )
    except ValueError as exc:
        console.print(f"[red]Invalid score config:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    ref = f"{config.name}@{config.version}"
    if existing is not None and config.content_digest == existing.content_digest:
        console.print(
            f"[yellow]Already registered[/yellow] {ref} (identical body — no-op)\n"
            f"  config_id={config.config_id}\n  digest={config.content_digest}"
        )
        return
    console.print(
        f"[green]Registered[/green] {ref}  {value_type.value}  {_shape_summary(config)}\n"
        f"  config_id={config.config_id}\n  digest={config.content_digest}"
    )


@config_app.command("list")
def config_list(
    all_versions: Annotated[
        bool, typer.Option("--all", help="Every version, not just the latest per name.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")] = False,
) -> None:
    """List score configurations (latest version per name by default)."""
    configs = list_configs(all_versions=all_versions)
    if as_json:
        payload = [json.loads(c.model_dump_json(exclude_none=True)) for c in configs]
        console.print_json(json.dumps(payload))
        return
    table = Table(title=f"score configs ({len(configs)})")
    for col in ("name", "version", "value_type", "shape", "content_digest", "description"):
        table.add_column(col, overflow="fold")
    for c in configs:
        table.add_row(
            c.name,
            str(c.version),
            c.value_type.value,
            _shape_summary(c),
            c.content_digest,
            c.description,
        )
    console.print(table)


def _resolve_or_exit(ref: str) -> ScoreConfig:
    try:
        return resolve_config_ref(ref)
    except (ScoreConfigNotFoundError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@config_app.command("get")
def config_get(
    ref: Annotated[str, typer.Argument(help="name | name@version | sha256:<hex>.")],
) -> None:
    """Resolve one config and print it as canonical JSON."""
    config = _resolve_or_exit(ref)
    console.print_json(config.model_dump_json(exclude_none=True))


@config_app.command("show")
def config_show(
    name: Annotated[str, typer.Argument(help="Metric name.")],
) -> None:
    """Human-readable view of a config: shape, digest, and version history."""
    try:
        latest = get_config(name)
    except ScoreConfigNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"[bold]{latest.name}[/bold]  ({latest.value_type.value})")
    console.print(f"  {latest.description}")
    console.print(f"  shape: {_shape_summary(latest)}")
    if latest.labels:
        console.print(f"  labels: {', '.join(latest.labels)}")
    history = [c for c in list_configs(all_versions=True) if c.name == name]
    table = Table(title="version history")
    for col in ("version", "content_digest", "created_at"):
        table.add_column(col, overflow="fold")
    for c in history:
        table.add_row(str(c.version), c.content_digest, c.created_at)
    console.print(table)
