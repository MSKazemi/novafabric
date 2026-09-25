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

"""``nova export-rocrate-science`` — FAIR Workflow-Run-RO-Crate science profile.

ADR-0164 NF-324 (experimental). Composes over the NF-040 ``nova export-rocrate``
carrier; exit ``1`` when the capsule cannot be exported honestly (no science
facet, tampered receipt, a profile needing an undeclared workflow, a malformed
DAG), ``2`` for usage errors (no such capsule, malformed DOI/ORCID/ROR).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

console = Console()
err_console = Console(stderr=True)

_PROFILES = ("process-run-crate", "workflow-run-crate")


def export_rocrate_science_cmd(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Output directory (default: the capsule's parent)."),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help=(
                "process-run-crate | workflow-run-crate. Default: workflow-run-crate when "
                "the receipt declares a workflow_digest, else process-run-crate."
            ),
        ),
    ] = None,
    doi: Annotated[str | None, typer.Option("--doi", help="DOI of the research object.")] = None,
    orcid: Annotated[
        list[str] | None,
        typer.Option("--orcid", help="Contributor ORCID iD (repeatable). Recorded as reference."),
    ] = None,
    ror: Annotated[
        list[str] | None,
        typer.Option("--ror", help="Organisation ROR id (repeatable)."),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", help="Print the fair_binding record as JSON.")
    ] = False,
) -> None:
    """Export a science capsule as a Workflow-Run-RO-Crate science profile (NF-324).

    Reuses the NF-040 RO-Crate v1.1 carrier and adds the Workflow Run Crate 0.5
    profile entities (W3C-PROV-aligned CreateAction, receipt digests and seeds,
    the hypothesis→claim DAG), binding the sealed science root. Writes
    ``<run_id>.science.rocrate.zip`` and ``<run_id>.fair-binding.json``.
    Deterministic: same capsule, same bytes. Experimental.

    \b
    Examples:
      nova export-rocrate-science --capsule runs/run_1 --out ./crates
      nova export-rocrate-science --capsule run_1 --orcid 0000-0002-1825-0097
      nova export-rocrate-science --capsule run_1 --doi 10.5281/zenodo.123 --json
    """
    from pydantic import ValidationError

    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
    from novafabric.science.fair_rocrate import (
        InvalidPersistentIdentifierError,
        PersistentIdentifiers,
        export_science_rocrate,
    )
    from novafabric.science.provenance import ScienceProvenanceError
    from novafabric.science.reproducibility import IN_MISSION_BOUNDARY

    if profile is not None and profile not in _PROFILES:
        err_console.print(f"[red]--profile must be one of {', '.join(_PROFILES)}[/red]")
        raise typer.Exit(2)
    try:
        capsule_dir = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    try:
        pids = PersistentIdentifiers(doi=doi, orcid_refs=orcid or [], ror_refs=ror or [])
    except InvalidPersistentIdentifierError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    out_dir = out if out is not None else capsule_dir.parent
    try:
        result = export_science_rocrate(
            capsule_dir,
            out_dir,
            profile=profile,  # type: ignore[arg-type]
            pids=pids,
        )
    except (ScienceProvenanceError, ValidationError) as exc:
        err_console.print(f"[red]x[/red] {exc}")
        console.print(f"[dim]{IN_MISSION_BOUNDARY}[/dim]", soft_wrap=True)
        raise typer.Exit(1) from exc

    if json_out:
        print(json.dumps(result.binding.model_dump(mode="json"), indent=2, sort_keys=True))
        raise typer.Exit(0)
    binding = result.binding
    console.print(f"[green]✓[/green] Science RO-Crate written to {result.crate_path}")
    console.print(f"  profile: {binding.rocrate_profile} ({', '.join(binding.profile_uris)})")
    console.print(f"  rocrate_digest: {binding.rocrate_digest}")
    console.print(f"  sealed_root: {binding.sealed_root or 'unbound'}")
    console.print(f"  fair_binding: {result.binding_path}")
    if binding.unbound:
        console.print(f"  unbound: {', '.join(binding.unbound)}")
    console.print("  prov_alignment: w3c-prov   verdict: null")
    console.print(f"[dim]{IN_MISSION_BOUNDARY}[/dim]", soft_wrap=True)
