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

P2 ships the ``receipt`` sub-group (NF-323); P3 ships ``lab show|verify``
(NF-322) and ``instrument show`` (NF-329). Every output carries the
in-mission-boundary line: NovaFabric records what would have to match to re-run a
computation and what a lab *declared*; it never re-executes a computation,
dispatches to a lab, or reads instrument telemetry.

**Exit codes.** ``0`` the command did its job. ``1`` ``verify`` found a receipt
that does not re-derive its root, misreports its incompleteness, is malformed,
or is absent — or ``--strict`` and the receipt is incomplete; ``lab verify``
found an unresolved instrument, a calibration after the experiment, a digest
that does not re-derive, or a malformed / absent block; ``show`` found a
malformed block. ``2`` usage or input error (no such capsule, malformed digest).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.markup import escape

from novafabric.capsule._manifest_write import FORCE_UNSEAL_FLAG, FORCE_UNSEAL_HELP

app = typer.Typer(
    name="science",
    help=(
        "Agentic-science provenance: reproducibility receipt, lab experiment and "
        "instrument provenance (experimental, ADR-0164 NF-322/323/329). Record-only — "
        "never re-executes, never controls a lab, never reads telemetry."
    ),
    no_args_is_help=True,
)
receipt_app = typer.Typer(
    name="receipt",
    help="Computational-reproducibility receipt (NF-323): build and verify, record-only.",
    no_args_is_help=True,
)
app.add_typer(receipt_app, name="receipt")
lab_app = typer.Typer(
    name="lab",
    help="Declared lab-experiment provenance (NF-322): show and verify, record-only.",
    no_args_is_help=True,
)
app.add_typer(lab_app, name="lab")
instrument_app = typer.Typer(
    name="instrument",
    help="Declared instrument / calibration provenance (NF-329): show, record-only.",
    no_args_is_help=True,
)
app.add_typer(instrument_app, name="instrument")

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


def _write_manifest(capsule_dir: Path, updated: dict[str, Any], *, force_unseal: bool) -> None:
    """Atomically replace capsule.yaml; a sealed capsule is refused (exit 2) unless forced."""
    from novafabric.capsule._manifest_write import (
        UNSEAL_WARNING,
        ManifestWriteError,
        write_capsule_manifest,
    )

    text = yaml.safe_dump(updated, sort_keys=False)
    try:
        result = write_capsule_manifest(capsule_dir, text, force_unseal=force_unseal)
    except ManifestWriteError as exc:
        err_console.print(f"[red]Refusing --write:[/red] {escape(str(exc))}", soft_wrap=True)
        raise typer.Exit(2) from exc
    if result.was_sealed:
        err_console.print(f"[bold red]{escape(UNSEAL_WARNING)}[/bold red]", soft_wrap=True)


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
    force_unseal: Annotated[bool, typer.Option(FORCE_UNSEAL_FLAG, help=FORCE_UNSEAL_HELP)] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the receipt as JSON.")] = False,
) -> None:
    """Build a reproducibility receipt (NF-323) — record-only, never re-executes.

    Binds every supplied digest and seed under one ``bound_root`` (plus the
    capsule's sealed science root when the facet records one). Components not
    supplied are named in ``receipt_incomplete`` — never fabricated.

    ``--write`` replaces capsule.yaml atomically. A NovaSeal-sealed capsule is
    refused (exit 2) unless ``--force-unseal`` is passed — the seal then no
    longer verifies and must be re-issued. A symlinked capsule.yaml is refused.

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
        _write_manifest(capsule_dir, attach_receipt(manifest, receipt), force_unseal=force_unseal)

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


# ── lab / instrument (NF-322, NF-329) ─────────────────────────────────────


def _lab_boundary() -> None:
    from novafabric.science.lab import LAB_BOUNDARY

    console.print(f"[dim]{LAB_BOUNDARY}[/dim]", soft_wrap=True)


def _load_lab(manifest: dict[str, Any]) -> Any:
    """Parse lab provenance, exiting 1 (with the boundary line) when malformed."""
    from pydantic import ValidationError

    from novafabric.science.lab import lab_from_capsule
    from novafabric.science.provenance import ScienceProvenanceError

    try:
        return lab_from_capsule(manifest)
    except (ValidationError, ScienceProvenanceError) as exc:
        # The message names the field and rule, never a rejected value.
        err_console.print(f"[red]Malformed lab / instrument provenance:[/red] {exc}")
        _lab_boundary()
        raise typer.Exit(1) from exc


def _instrument_rows(lab: Any) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json", exclude_none=True) for r in lab.instruments]


@lab_app.command("show")
def lab_show(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit the blocks as JSON.")] = False,
) -> None:
    """Print a capsule's declared lab experiment and its instruments (NF-322).

    Declarations only: NovaFabric never dispatched, scheduled or controlled the
    experiment, and never read instrument telemetry.

    \b
    Examples:
      nova science lab show --capsule runs/run_1
      nova science lab show --capsule run_1 --json
    """
    lab = _load_lab(_read_manifest(_capsule_dir(capsule)))
    if json_out:
        body: dict[str, Any] = {
            "lab_experiment": None,
            "instrument_provenance": [],
            "controls_lab": False,
            "reads_telemetry": False,
        }
        if lab is not None:
            if lab.experiment is not None:
                body["lab_experiment"] = lab.experiment.model_dump(mode="json", exclude_none=True)
            body["instrument_provenance"] = _instrument_rows(lab)
        print(json.dumps(body, indent=2, sort_keys=True))
        raise typer.Exit(0)
    if lab is None or lab.experiment is None:
        console.print("No lab_experiment in this capsule.")
    else:
        exp = lab.experiment
        console.print(f"lab_kind: {exp.lab_kind}   sim_to_real: {exp.sim_to_real}")
        console.print(f"run_id: {exp.run_id}", markup=False)
        console.print(f"protocol_ref: {exp.protocol_ref}")
        console.print(f"outcome_digest: {exp.outcome_digest}")
        console.print(f"started_at: {exp.started_at or '(not declared)'}")
        console.print(f"node_ref: {exp.node_ref or '(none)'}")
        console.print(f"instrument_refs: {len(exp.instrument_refs)}")
        console.print(f"experiment_digest: {exp.experiment_digest}")
    if lab is not None and lab.instruments:
        console.print(f"instruments recorded: {len(lab.instruments)}")
    _lab_boundary()


@lab_app.command("verify")
def lab_verify(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the verification as JSON.")
    ] = False,
) -> None:
    """Offline-verify a capsule's declared lab experiment (NF-322 / NF-329).

    Fails (exit 1) when an instrument_ref resolves to no instrument record, an
    instrument was calibrated after the experiment (or the experiment time is
    unknown), a digest does not re-derive, a ``simulation`` is declared
    ``real``, the ``node_ref`` names no experiment node, or the block is absent
    or malformed (unknown ``lab_kind``, bad digest, telemetry-shaped field).
    Checks coherence of declarations only: ``verdict: null``.

    \b
    Examples:
      nova science lab verify --capsule runs/run_1
      nova science lab verify --capsule run_1 --json
    """
    from novafabric.science.lab import verify_lab

    manifest = _read_manifest(_capsule_dir(capsule))
    lab = _load_lab(manifest)
    if lab is None or lab.experiment is None:
        if json_out:
            print(json.dumps({"lab_experiment": None, "verdict": None}, indent=2))
        else:
            console.print("No lab_experiment in this capsule.")
            _lab_boundary()
        raise typer.Exit(1)

    result = verify_lab(lab, capsule=manifest)
    if json_out:
        body = result.model_dump(mode="json")
        body["ok"] = result.ok
        print(json.dumps(body, indent=2, sort_keys=True))
        raise typer.Exit(0 if result.ok else 1)

    def _mark(flag: bool | None) -> str:
        if flag is None:
            return "[dim]not applicable[/dim]"
        return "[green]ok[/green]" if flag else "[red]FAIL[/red]"

    console.print(f"experiment_digest_ok: {_mark(result.experiment_digest_ok)}")
    console.print(f"instrument_digests_ok: {_mark(result.instrument_digests_ok)}")
    for name in result.tampered_instruments:
        console.print(f"  tampered: {name}", markup=False)
    console.print(f"instrument_refs_resolve: {_mark(result.instrument_refs_resolve)}")
    for ref in result.unresolved_instrument_refs:
        console.print(f"  unresolved: {ref}")
    console.print(
        f"calibration_not_after_experiment: {_mark(result.calibration_not_after_experiment)}"
    )
    console.print(
        f"  experiment_time: {result.experiment_time or 'unknown'} "
        f"({result.experiment_time_source or 'no started_at / created_at'})"
    )
    for name in result.calibrated_after_experiment:
        console.print(f"  calibrated after experiment: {name}", markup=False)
    console.print(f"sim_to_real_consistent: {_mark(result.sim_to_real_consistent)}")
    console.print(f"lineage_node_resolves: {_mark(result.lineage_node_resolves)}")
    if result.unreferenced_instruments:
        console.print(
            "unreferenced instruments (informational): "
            + ", ".join(result.unreferenced_instruments),
            markup=False,
        )
    console.print("controls_lab: false   reads_telemetry: false   verdict: null")
    _lab_boundary()
    raise typer.Exit(0 if result.ok else 1)


@instrument_app.command("show")
def instrument_show(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the instrument records as JSON.")
    ] = False,
) -> None:
    """Print a capsule's declared instrument / calibration provenance (NF-329).

    Firmware and calibration are *declared* digests and timestamps; NovaFabric
    never contacts the instrument or reads its telemetry.

    \b
    Examples:
      nova science instrument show --capsule runs/run_1
      nova science instrument show --capsule run_1 --json
    """
    lab = _load_lab(_read_manifest(_capsule_dir(capsule)))
    rows = _instrument_rows(lab) if lab is not None else []
    if json_out:
        body = {"instrument_provenance": rows, "reads_telemetry": False}
        print(json.dumps(body, indent=2, sort_keys=True))
        raise typer.Exit(0)
    if not rows:
        console.print("No instrument_provenance in this capsule.")
    for row in rows:
        console.print(f"{row['instrument_id']} ({row['instrument_class']})", markup=False)
        console.print(f"  firmware_digest: {row['firmware_digest']}")
        console.print(f"  calibration_ref: {row['calibration_ref']}")
        console.print(f"  calibration_timestamp: {row['calibration_timestamp']}")
        console.print(f"  manufacturer_ref: {row['manufacturer_ref']}", markup=False)
        console.print(f"  record_digest: {row['record_digest']}")
    _lab_boundary()
