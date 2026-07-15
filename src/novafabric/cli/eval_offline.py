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

"""``nova eval offline`` — trace-first zero-token offline eval (NF-009, ADR-0099).

Runs a structural check over a stored capsule with **zero model calls** and prints (or
seals) a ``code`` ``Score``:

    nova eval offline --capsule <dir> --check coverage --declared-tools a,b,c [--emit-score]
    nova eval offline --capsule <dir> --check contract --schema schema.json [--field output]
    nova eval offline --capsule <dir> --check metamorphic --spec check-spec.yaml [--emit-score]

``--emit-score`` appends the Score to ``<capsule>/scores.jsonl`` so it is sealed by the
capsule Merkle root. The ``metamorphic`` check is driven by a declarative check-spec
(``schemas/features/metamorphic-check-v0.schema.json``) passed via ``--spec``.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console

from novafabric.eval.offline import (
    MetamorphicSpecError,
    run_contract,
    run_coverage,
    run_metamorphic_spec,
)
from novafabric.eval.scores import SCORES_FILENAME, append_score

console = Console()


class OfflineCheck(str, Enum):
    coverage = "coverage"
    contract = "contract"
    metamorphic = "metamorphic"


def offline_cmd(
    capsule: Annotated[Path, typer.Option(help="Capsule directory to check.")],
    check: Annotated[OfflineCheck, typer.Option(help="coverage|contract|metamorphic.")],
    declared_tools: Annotated[
        str | None, typer.Option(help="Comma-separated declared tools (coverage).")
    ] = None,
    schema: Annotated[
        Path | None, typer.Option(help="JSON-schema file (contract).")
    ] = None,
    spec: Annotated[
        Path | None,
        typer.Option(help="Metamorphic check-spec YAML/JSON (metamorphic)."),
    ] = None,
    field: Annotated[str, typer.Option(help="Record field to validate (contract).")] = "output",
    records_file: Annotated[
        str, typer.Option(help="JSONL file within the capsule (contract).")
    ] = "tool-calls.jsonl",
    emit_score: Annotated[
        bool, typer.Option("--emit-score", help="Append the Score to <capsule>/scores.jsonl.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the Score as JSON.")] = False,
) -> None:
    """Run a zero-token structural check over a stored capsule (NF-009)."""
    if not capsule.is_dir():
        raise typer.BadParameter(f"not a capsule directory: {capsule}")

    if check is OfflineCheck.coverage:
        if not declared_tools:
            raise typer.BadParameter("coverage requires --declared-tools")
        tools = [t.strip() for t in declared_tools.split(",") if t.strip()]
        score = run_coverage(capsule, tools)
    elif check is OfflineCheck.contract:
        if schema is None:
            raise typer.BadParameter("contract requires --schema")
        schema_obj = json.loads(schema.read_text(encoding="utf-8"))
        score = run_contract(capsule, schema_obj, field=field, records_file=records_file)
    else:  # metamorphic
        if spec is None:
            raise typer.BadParameter("metamorphic requires --spec")
        spec_obj = yaml.safe_load(spec.read_text(encoding="utf-8"))
        if not isinstance(spec_obj, dict):
            raise typer.BadParameter("check-spec must be a mapping")
        try:
            score = run_metamorphic_spec(capsule, spec_obj)
        except MetamorphicSpecError as exc:
            raise typer.BadParameter(str(exc)) from exc

    if as_json:
        console.print_json(score.model_dump_json(exclude_none=True))
    else:
        console.print(f"{score.name} = {score.value}  (source={score.source.value}, zero-token)")

    if emit_score:
        append_score(capsule / SCORES_FILENAME, score)
        console.print(f"[green]Recorded[/green] {score.score_id} → {capsule / SCORES_FILENAME}")
