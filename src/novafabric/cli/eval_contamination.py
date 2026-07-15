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

"""``nova eval contamination-check`` — benchmark-contamination flag (NF-028, ADR-0108).

Compares a capsule's recorded ``dataset_provenance`` facets against a configurable
known-contaminated/superseded hash registry and reports a status per dataset. Exits
``4`` when any dataset is contaminated or superseded (so CI can gate on it), ``0`` when
all datasets are current/unknown, ``2`` on a usage error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

console = Console()

_STATUS_STYLE = {
    "contaminated": "red",
    "superseded": "yellow",
    "current": "green",
    "unknown": "dim",
}


def contamination_check_cmd(
    capsule: Annotated[
        Path, typer.Argument(help="Capsule directory to check for benchmark contamination.")
    ],
    registry: Annotated[
        Optional[Path],
        typer.Option(
            "--registry",
            help="JSON registry of known-contaminated/superseded sha256 hashes "
            '({"contaminated": [...], "superseded": [...]}). Configurable; no default URL.',
        ),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the per-dataset results as JSON.")
    ] = False,
) -> None:
    """Flag a capsule run against a contaminated or superseded benchmark (NF-028).

    Scope: single capsule.

    \b
    Examples:
      nova eval contamination-check ./my-capsule
      nova eval contamination-check ./my-capsule --registry known-bad.json --json
    """
    import json as _json

    from novafabric.eval.dataset_provenance import (
        ContaminationRegistry,
        as_dicts,
        check_contamination,
        is_flagged,
    )

    if not capsule.is_dir():
        raise typer.BadParameter(f"not a capsule directory: {capsule}")

    reg: ContaminationRegistry | None = None
    if registry is not None:
        if not registry.is_file():
            raise typer.BadParameter(f"registry file not found: {registry}")
        try:
            reg = ContaminationRegistry.from_path(registry)
        except (ValueError, OSError) as exc:
            raise typer.BadParameter(f"could not parse registry: {exc}") from exc

    results = check_contamination(capsule, reg)

    if as_json:
        console.print_json(_json.dumps(as_dicts(results)))
    elif not results:
        console.print("[dim]No dataset_provenance facets in this capsule.[/dim]")
    else:
        table = Table(title="Benchmark contamination check")
        table.add_column("Dataset", style="bold")
        table.add_column("Version")
        table.add_column("Status")
        for r in results:
            style = _STATUS_STYLE.get(r.status, "")
            table.add_row(r.name, r.version or "-", f"[{style}]{r.status}[/{style}]")
        console.print(table)

    if is_flagged(results):
        raise typer.Exit(code=4)
