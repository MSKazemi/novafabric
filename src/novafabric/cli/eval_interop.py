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

"""``nova eval import-inspect`` / ``export-inspect`` — Inspect-AI log interop (NF-024).

Score-level bridge (ADR-0108): import an Inspect AI JSON eval log's scorer
results into a capsule's ``scores.jsonl`` (provenance-stamped, unmapped fields
preserved under ``extensions/org.inspect/``), and export a capsule's score log
as an Inspect-compatible JSON log. Pure stdlib parsing — no ``inspect-ai``
dependency.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()


def import_inspect_cmd(
    log: Annotated[
        Path, typer.Argument(help="Inspect AI eval log (JSON log format).")
    ],
    capsule: Annotated[
        Path,
        typer.Option(
            "--capsule",
            help="Capsule directory to import scores into (created if missing).",
        ),
    ],
) -> None:
    """Import an Inspect-AI JSON eval log's scores into a capsule (NF-024).

    Maps each sample scorer result to an evidence-grade Score in the capsule's
    scores.jsonl (source stamped inspect-ai). Fields with no Score target are
    preserved — never silently dropped — in extensions/org.inspect/import.json,
    and a conformance summary is printed.

    \b
    Examples:
      nova eval import-inspect ./logs/hello.json --capsule ./my-capsule
    """
    from novafabric.eval.dataset_provenance import DatasetProvenanceFacet, write_facet
    from novafabric.eval.inspect_interop import (
        IMPORT_RECORD_PATH,
        InspectLogError,
        import_inspect_log,
    )
    from novafabric.eval.scores import SCORES_FILENAME, append_score

    try:
        result = import_inspect_log(log)
    except (FileNotFoundError, InspectLogError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    capsule.mkdir(parents=True, exist_ok=True)
    for score in result.scores:
        append_score(capsule / SCORES_FILENAME, score)

    # Preserve the honesty ledger under the org.inspect extension namespace.
    record_path = capsule / IMPORT_RECORD_PATH
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(result.model_dump(exclude={"scores"}), indent=2, default=str),
        encoding="utf-8",
    )

    # NF-028 tie-in: record the dataset facet (no hashes in Inspect logs → unknown).
    if result.provenance.dataset_name:
        write_facet(
            capsule,
            DatasetProvenanceFacet(name=result.provenance.dataset_name, status="unknown"),
        )

    prov = result.provenance
    console.print(
        f"Imported [bold]{len(result.scores)}[/bold] score(s) from Inspect log "
        f"[bold]{prov.task or log.name}[/bold] (model: {prov.model or '-'}) "
        f"into {capsule / SCORES_FILENAME}"
    )
    console.print(
        f"Conformance: mapping v{prov.mapping_version}, log version {prov.log_version}; "
        f"{len(result.unmapped)} unmapped field(s) preserved, "
        f"{len(result.omitted)} content field(s) omitted (not copied) — "
        f"see {record_path}"
    )


def export_inspect_cmd(
    capsule: Annotated[
        Path, typer.Argument(help="Capsule directory whose scores.jsonl to export.")
    ],
    output: Annotated[
        Optional[Path],
        typer.Option("--output", "-o", help="Write the Inspect JSON log here (default: stdout)."),
    ] = None,
) -> None:
    """Export a capsule's scores.jsonl as an Inspect-compatible JSON log (NF-024).

    A capsule without scores exports a valid empty log. If the capsule was
    itself imported from Inspect, the preserved header (task/model/run id)
    is restored.

    \b
    Examples:
      nova eval export-inspect ./my-capsule -o inspect-log.json
    """
    from novafabric.eval.inspect_interop import export_inspect_log

    if not capsule.is_dir():
        raise typer.BadParameter(f"not a capsule directory: {capsule}")

    log = export_inspect_log(capsule)
    payload = json.dumps(log, indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload + "\n", encoding="utf-8")
        console.print(f"Wrote Inspect log ({len(log['samples'])} sample(s)) to {output}")
    else:
        console.print_json(payload)
