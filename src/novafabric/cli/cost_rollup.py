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

"""nova cost rollup — acted-as delegation cost rollup (ADR-0146 P2 / NF-142).

Read-only and report-only: loads a delegation document (the
:mod:`novafabric.trust.delegation` grant shape) and an NF-141
``cost_attribution`` facet (from a capsule directory / run id, or a JSON/YAML
file), and prints each hop's ``self_cost`` / ``subtree_cost`` plus the
conservation block. Nothing is written — ``cost_rollup`` is not a registered
capsule facet (ADR-0196 D2).

Exit codes: 0 — report rendered (including ``basis: partial`` and structural
findings such as a cycle or a broken chain); 2 — an input is missing,
oversized, or malformed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.markup import escape

console = Console()
err_console = Console(stderr=True)

#: Largest input file read (either argument). Real inputs are kilobytes.
MAX_INPUT_BYTES = 8 * 1024 * 1024
_MANIFEST_NAME = "capsule.yaml"


def _fail(message: str) -> typer.Exit:
    """Print ``message`` to stderr and return the exit-2 signal for the caller to raise."""
    err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(2)


def _read_doc(path: Path) -> Any:
    """Read a bounded JSON (or YAML, by suffix) document; exit 2 on any failure."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _fail(f"Could not read {path}: {exc}") from exc
    if size > MAX_INPUT_BYTES:
        raise _fail(f"{path} is {size} bytes (cap {MAX_INPUT_BYTES})")
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(text)
        return json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise _fail(f"Could not parse {path}: {exc}") from exc


def _attribution_source(ref: str) -> Path:
    """A file is read as-is; anything else is resolved as a capsule dir / run id."""
    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref

    candidate = Path(ref)
    if candidate.is_file():
        return candidate
    try:
        return resolve_capsule_ref(ref) / _MANIFEST_NAME
    except CapsuleRefError as exc:
        raise _fail(str(exc)) from exc


def _money(value: object, currency: str | None) -> str:
    if value is None:
        return "-"
    return f"{value} {currency}" if currency else str(value)


def cost_rollup_cmd(
    delegation: Annotated[
        Path,
        typer.Argument(help="Delegation JSON: {grants:[…]} and/or {chains:[{grants:[…]}]}."),
    ],
    attribution: Annotated[
        str,
        typer.Argument(
            help="Capsule dir or run id (reads facets.cost_attribution), or a JSON/YAML file "
            "holding the facet."
        ),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the rollup report as JSON."),
    ] = False,
) -> None:
    """Roll per-agent cost up the acted-as delegation chain — report-only.

    Each hop gets its own attributed cost (self) and its subtree cost (self plus
    every grantee's subtree). Conservation compares the root subtree(s) with the
    run total exactly (Decimal, no epsilon) and names unchained and unattributed
    cost. Cycles and broken chains are reported as findings (basis: partial).
    Grant signatures are NOT re-verified here. Nothing is written to the capsule.

    \b
    Examples:
      nova cost rollup delegation.json ./my-capsule
      nova cost rollup delegation.json 01KZ8Q… --json
      nova cost rollup delegation.json attribution.json
    """
    from novafabric.cost.rollup import (
        RollupError,
        build_rollup,
        parse_attribution,
        parse_delegation,
    )

    if not delegation.is_file():
        raise _fail(f"Delegation document not found: {delegation}")
    delegation_doc = _read_doc(delegation)
    attribution_doc = _read_doc(_attribution_source(attribution))
    try:
        report = build_rollup(parse_delegation(delegation_doc), parse_attribution(attribution_doc))
    except RollupError as exc:
        raise _fail(f"{type(exc).__name__}: {exc}") from exc

    if json_out:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        raise typer.Exit(0)

    cons = report.conservation
    cur = cons.currency
    console.print(
        f"Acted-as cost rollup (NF-142) — basis: [bold]{report.basis}[/bold], "
        "signatures not re-verified"
    )
    for hop in report.hops:
        grantees = ", ".join(hop.grantees) or "-"
        console.print(
            f"  {'  ' * hop.depth}{escape(hop.agent_id)}  self={_money(hop.self_cost, cur)}  "
            f"subtree={_money(hop.subtree_cost, cur)}  grantees={escape(f'[{grantees}]')}"
        )
    for agent in report.unchained:
        console.print(
            f"  (unchained) {escape(agent.agent_id)}  self={_money(agent.self_cost, cur)}"
        )
    console.print(
        f"Conservation: root_subtree={_money(cons.root_subtree_cost, cur)}  "
        f"run_total={_money(cons.run_total_cost, cur)}  "
        f"unchained={_money(cons.unchained_cost, cur)}  "
        f"unattributed={_money(cons.unattributed_cost, cur)}  ok={cons.ok}"
    )
    for finding in report.findings:
        console.print(f"  [yellow]finding[/yellow] {finding.code}: {escape(finding.detail)}")
    console.print(report.record_only)
    raise typer.Exit(0)
