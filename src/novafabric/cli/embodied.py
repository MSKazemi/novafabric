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

"""``nova embodied`` — read and verify ``facets.embodied`` offline (ADR-0162).

Experimental. P2 ships two subcommands: ``odd show`` (NF-303) and
``trajectory verify`` (NF-310). Both read the capsule manifest only — no robot,
network, or control-plane contact — and every output carries the
in-mission-boundary line: NovaFabric records; it never actuates or adjudicates.

**Exit codes are a contract.**

- ``0`` — the command did its job: the ODD record was shown (including "none
  recorded"), or the trajectory chain was walked and is intact.
- ``1`` — the recorded evidence is defective: a trajectory with a broken,
  cyclic or out-of-order link; or an ODD block that carries a ruling
  (non-null ``verdict``, ``in_odd: true``), a raw payload, a non-digest ref,
  or out-of-order excursions.
- ``2`` — nothing could be checked: the capsule was not found or not readable,
  or (``trajectory verify`` only) no trajectory is recorded. A verifier that
  exits ``0`` on an absent chain would pass exactly the capsules with nothing
  to check.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

embodied_app = typer.Typer(
    name="embodied",
    help=(
        "Embodied/cyber-physical evidence: ODD record + trajectory chain, offline "
        "(experimental, ADR-0162). Record-only — never actuates or adjudicates."
    ),
    no_args_is_help=True,
)
odd_app = typer.Typer(
    name="odd",
    help="NF-303 declared ODD + observed excursions (verdict always null).",
    no_args_is_help=True,
)
trajectory_app = typer.Typer(
    name="trajectory",
    help="NF-310 perception→world_model→decision→actuation chain of digests.",
    no_args_is_help=True,
)
embodied_app.add_typer(odd_app, name="odd")
embodied_app.add_typer(trajectory_app, name="trajectory")

console = Console()
err_console = Console(stderr=True)

_MANIFEST_NAME = "capsule.yaml"

CapsuleOpt = Annotated[
    str,
    typer.Option(
        "--capsule",
        help="Capsule directory, or a bare run id under the capsule store.",
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _load_embodied(capsule: str) -> dict[str, Any] | None:
    """Return the raw ``facets.embodied`` mapping, or ``None`` when absent.

    Exits ``2`` when the capsule cannot be found or read. Raw rather than
    validated so each subcommand validates only the object it reports on: a
    defect in one embodied object must not hide the evidence in another.
    """
    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref

    try:
        capsule_dir = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    try:
        manifest = yaml.safe_load((capsule_dir / _MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        err_console.print(f"[red]Could not read {_MANIFEST_NAME}:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc
    if not isinstance(manifest, dict):
        err_console.print(f"[red]{_MANIFEST_NAME} is not a mapping.[/red]")
        raise typer.Exit(2)
    facets = manifest.get("facets")
    embodied = facets.get("embodied") if isinstance(facets, dict) else None
    return embodied if isinstance(embodied, dict) else None


def _boundary() -> str:
    from novafabric.embodied import IN_MISSION_BOUNDARY

    return IN_MISSION_BOUNDARY


def _emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _defect(json_out: bool, kind: str, exc: Exception) -> None:
    """Report a defective recorded object and exit ``1``."""
    name = type(exc).__name__
    if json_out:
        _emit_json({"ok": False, "error": name, "detail": str(exc), "boundary": _boundary()})
    else:
        err_console.print(f"[red]Defective {kind} record ({name}):[/red] {escape(str(exc))}")
        console.print(_boundary())
    raise typer.Exit(1)


# ── nova embodied odd show ────────────────────────────────────────────────


@odd_app.command("show")
def odd_show_cmd(capsule: CapsuleOpt, json_out: JsonOpt = False) -> None:
    """Show the declared ODD and observed excursions (NF-303). Verdict is always null.

    Exits 1 if the recorded block carries a ruling (non-null ``verdict`` or an
    excursion marked ``in_odd: true``) or is otherwise malformed; 2 if the
    capsule cannot be read. No ODD recorded is reported, not failed.

    \b
    Examples:
      nova embodied odd show --capsule ./capsules/run-01HX
      nova embodied odd show --capsule 01HXAY7M5JZ8R7K4P9DPBYK2WX --json
    """
    from novafabric.embodied import (
        AdjudicationRefusedError,
        ExcursionOrderError,
        InvalidReferenceError,
        InvalidTimestampError,
        OddConformance,
        RawPayloadRejectedError,
    )

    embodied = _load_embodied(capsule)
    raw = None if embodied is None else embodied.get("odd")
    if raw is None:
        if json_out:
            _emit_json({"recorded": False, "boundary": _boundary()})
        else:
            console.print(
                "No ODD record in this capsule — nothing was recorded, which is "
                "not a finding about the system's operating domain."
            )
            console.print(_boundary())
        return
    try:
        odd = OddConformance.model_validate(raw)
    except (
        AdjudicationRefusedError,
        ExcursionOrderError,
        InvalidReferenceError,
        InvalidTimestampError,
        RawPayloadRejectedError,
        ValidationError,
    ) as exc:
        _defect(json_out, "ODD", exc)
        return  # pragma: no cover - _defect always exits

    if json_out:
        _emit_json(
            {
                "recorded": True,
                "odd": odd.model_dump(exclude_none=True),
                "excursion_count": len(odd.excursions),
                "boundary": _boundary(),
            }
        )
        return
    console.print(f"odd_ref: {odd.odd_ref}")
    console.print(f"excursions: {len(odd.excursions)}")
    for index, excursion in enumerate(odd.excursions):
        console.print(
            f"  [{index}] {escape(excursion.ts)}  {escape(excursion.condition)}: "
            f"{escape(excursion.observed)}  (in_odd: false)"
        )
    if not odd.excursions:
        console.print(
            "  none recorded — not a finding that the system stayed inside its declared ODD"
        )
    console.print(
        "verdict: null — NovaFabric records declared bounds and observed "
        "excursions; the safety judgement is the operator's assurance case."
    )
    console.print(_boundary())


# ── nova embodied trajectory verify ───────────────────────────────────────


@trajectory_app.command("verify")
def trajectory_verify_cmd(capsule: CapsuleOpt, json_out: JsonOpt = False) -> None:
    """Walk the perception→actuation chain offline (NF-310): acyclic, no broken parent.

    Exits 0 when the chain is intact, 1 naming each broken/cyclic/out-of-order
    hop (or a malformed hop), 2 when no trajectory is recorded or the capsule
    cannot be read. Stage regressions are reported as warnings, never fatal.

    \b
    Examples:
      nova embodied trajectory verify --capsule ./capsules/run-01HX
      nova embodied trajectory verify --capsule 01HXAY7M5JZ8R7K4P9DPBYK2WX --json
    """
    from novafabric.embodied import (
        InvalidReferenceError,
        InvalidTimestampError,
        RawPayloadRejectedError,
        TrajectoryHop,
        walk_trajectory,
    )

    embodied = _load_embodied(capsule)
    raw = None if embodied is None else embodied.get("trajectory")
    if raw is None:
        if json_out:
            _emit_json({"recorded": False, "ok": False, "boundary": _boundary()})
        else:
            err_console.print(
                "No trajectory recorded in this capsule — nothing to verify (absent is not a pass)."
            )
            console.print(_boundary())
        raise typer.Exit(2)
    try:
        if not isinstance(raw, list):
            raise TypeError("facets.embodied.trajectory must be a list of hops")
        hops = [TrajectoryHop.model_validate(hop) for hop in raw]
    except (
        InvalidReferenceError,
        InvalidTimestampError,
        RawPayloadRejectedError,
        TypeError,
        ValidationError,
    ) as exc:
        _defect(json_out, "trajectory", exc)
        return  # pragma: no cover - _defect always exits

    report = walk_trajectory(hops)
    if json_out:
        _emit_json(
            {
                "recorded": True,
                "ok": report.ok,
                **report.model_dump(),
                "boundary": _boundary(),
            }
        )
    else:
        status = "OK" if report.ok else "BROKEN"
        console.print(f"trajectory: {report.hop_count} hop(s) — {status}")
        console.print(
            f"  acyclic: {report.acyclic}  no_broken_parent: {report.no_broken_parent}  "
            f"monotonic: {report.monotonic}  complete: {report.complete}"
        )
        for finding in report.findings:
            where = "chain" if finding.hop_index is None else f"hop {finding.hop_index}"
            console.print(
                f"  {finding.severity.upper()} {finding.code} ({where}): {escape(finding.message)}"
            )
        console.print(
            "The chain is declared/observed artifact digests; NovaFabric walked it "
            "and re-derived nothing."
        )
        console.print(_boundary())
    if not report.ok:
        raise typer.Exit(1)


__all__ = ["embodied_app"]
