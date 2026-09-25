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

"""``nova science`` — agentic-science provenance evidence (ADR-0164, experimental).

P2 ships the ``receipt`` sub-group (NF-323). Every output carries the
in-mission-boundary line: NovaFabric records what would have to match to re-run a
computation; it never re-executes it.

**Exit codes.** ``0`` the command did its job. ``1`` ``verify`` found a receipt
that does not re-derive its root, misreports its incompleteness, is malformed,
or is absent — or ``--strict`` and the receipt is incomplete. ``2`` usage or
input error (no such capsule, malformed digest).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console

app = typer.Typer(
    name="science",
    help=(
        "Agentic-science provenance: reproducibility receipt (experimental, "
        "ADR-0164 NF-323). Record-only — never re-executes."
    ),
    no_args_is_help=True,
)
receipt_app = typer.Typer(
    name="receipt",
    help="Computational-reproducibility receipt (NF-323): build and verify, record-only.",
    no_args_is_help=True,
)
app.add_typer(receipt_app, name="receipt")

console = Console()
err_console = Console(stderr=True)

_MANIFEST_NAME = "capsule.yaml"
_DETERMINISM = ("bitwise", "statistical", "nondeterministic", "undeclared")


def _capsule_dir(ref: str) -> Path:
    """Resolve a capsule path or run id, exiting 2 when neither matches."""
    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref

    try:
        return resolve_capsule_ref(ref)
    except CapsuleRefError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc


def _read_manifest(capsule_dir: Path) -> dict[str, Any]:
    path = capsule_dir / _MANIFEST_NAME
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        err_console.print(f"[red]Could not read {_MANIFEST_NAME}:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(data, dict):
        err_console.print(f"[red]{_MANIFEST_NAME} is not a mapping.[/red]")
        raise typer.Exit(2)
    return data


def _boundary() -> None:
    from novafabric.science.reproducibility import IN_MISSION_BOUNDARY

    console.print(f"[dim]{IN_MISSION_BOUNDARY}[/dim]", soft_wrap=True)


@receipt_app.command("build")
def receipt_build(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    env: Annotated[
        str | None,
        typer.Option("--env", help="Environment digest (container/lockfile), sha256:<hex>."),
    ] = None,
    data: Annotated[
        str | None,
        typer.Option("--data", help="Input-data digest, sha256:<hex> (never the data)."),
    ] = None,
    code: Annotated[str | None, typer.Option("--code", help="Code digest, sha256:<hex>.")] = None,
    workflow: Annotated[
        str | None,
        typer.Option("--workflow", help="Optional workflow (RO-Crate run) digest, sha256:<hex>."),
    ] = None,
    seed: Annotated[
        list[int] | None,
        typer.Option("--seed", help="RNG seed; repeat for an ordered seed list."),
    ] = None,
    determinism: Annotated[
        str,
        typer.Option(
            "--determinism",
            help="Declared determinism class: bitwise|statistical|nondeterministic|undeclared.",
        ),
    ] = "undeclared",
    write: Annotated[
        bool, typer.Option("--write", help="Persist the receipt into capsule.yaml.")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the receipt as JSON.")] = False,
) -> None:
    """Build a reproducibility receipt (NF-323) — record-only, never re-executes.

    Binds every supplied digest and seed under one ``bound_root`` (plus the
    capsule's sealed science root when the facet records one). Components not
    supplied are named in ``receipt_incomplete`` — never fabricated.

    \b
    Examples:
      nova science receipt build --capsule run_1 --env sha256:<hex> --seed 1337
      nova science receipt build --capsule run_1 --code sha256:<hex> --write
    """
    from novafabric.science.provenance import ScienceProvenanceError, facet_from_capsule
    from novafabric.science.reproducibility import attach_receipt, build_receipt

    if determinism not in _DETERMINISM:
        err_console.print(f"[red]--determinism must be one of {', '.join(_DETERMINISM)}[/red]")
        raise typer.Exit(2)
    capsule_dir = _capsule_dir(capsule)
    manifest = _read_manifest(capsule_dir)
    try:
        facet = facet_from_capsule(manifest)
        receipt = build_receipt(
            environment_digest=env,
            seeds=seed or [],
            data_digest=data,
            code_digest=code,
            workflow_digest=workflow,
            determinism_class=determinism,  # type: ignore[arg-type]
            capsule_root=facet.bound_root if facet is not None else None,
        )
    except ScienceProvenanceError as exc:
        err_console.print(f"[red]Invalid receipt input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if write:
        updated = attach_receipt(manifest, receipt)
        (capsule_dir / _MANIFEST_NAME).write_text(
            yaml.safe_dump(updated, sort_keys=False), encoding="utf-8"
        )

    body = receipt.model_dump(mode="json", exclude_none=True)
    if json_out:
        print(json.dumps(body, indent=2, sort_keys=True))
        raise typer.Exit(0)
    console.print(f"bound_root: {receipt.bound_root}")
    console.print(f"determinism_class: {receipt.determinism_class}")
    console.print(f"receipt_incomplete: {receipt.receipt_incomplete or '[]'}")
    if write:
        console.print(
            f"[green]Wrote[/green] facets.science_provenance.reproducibility_receipt "
            f"to {_MANIFEST_NAME}"
        )
    else:
        console.print("[dim]Not written. Pass --write to persist.[/dim]")
    _boundary()


@receipt_app.command("verify")
def receipt_verify(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Also exit 1 when the receipt is incomplete."),
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the verification as JSON.")
    ] = False,
) -> None:
    """Offline-verify a capsule's reproducibility receipt (NF-323).

    Recomputes ``bound_root`` from the recorded components and checks that
    ``receipt_incomplete`` names exactly what is absent. Never re-executes and
    never asserts the run *is* reproducible (``reproducible_in_fact: null``).

    \b
    Examples:
      nova science receipt verify --capsule runs/run_1
      nova science receipt verify --capsule run_1 --strict --json
    """
    from pydantic import ValidationError

    from novafabric.science.provenance import ScienceProvenanceError
    from novafabric.science.reproducibility import receipt_from_capsule, verify_receipt

    capsule_dir = _capsule_dir(capsule)
    manifest = _read_manifest(capsule_dir)
    try:
        receipt = receipt_from_capsule(manifest)
    except (ValidationError, ScienceProvenanceError) as exc:
        err_console.print(f"[red]Malformed reproducibility_receipt:[/red] {exc}")
        _boundary()
        raise typer.Exit(1) from exc
    if receipt is None:
        if json_out:
            print(json.dumps({"reproducibility_receipt": None}, indent=2))
        else:
            console.print("No reproducibility_receipt in this capsule.")
            _boundary()
        raise typer.Exit(1)

    result = verify_receipt(receipt)
    failed = not result.ok or (strict and not result.complete)
    if json_out:
        print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
        raise typer.Exit(1 if failed else 0)

    mark = "[green]ok[/green]" if result.sealed_into_root else "[red]MISMATCH[/red]"
    console.print(f"sealed_into_root: {mark}")
    console.print(f"  declared: {result.declared_root}")
    console.print(f"  expected: {result.expected_root}")
    console.print(f"incomplete_declared_correctly: {result.incomplete_declared_correctly}")
    console.print(f"receipt_incomplete: {result.receipt_incomplete or '[]'}")
    console.print(f"determinism_class: {result.determinism_class}")
    console.print("re_executed: false   reproducible_in_fact: null")
    _boundary()
    raise typer.Exit(1 if failed else 0)
