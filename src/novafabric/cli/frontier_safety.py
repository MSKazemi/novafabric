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

"""``nova safety`` — frontier-safety evidence readers (ADR-0167 P2/P3, experimental).

Three read-only subcommands over the ``facets.frontier_safety`` block:

- ``nova safety control show``  — NF-352 external AI-control-protocol decisions.
- ``nova safety tripwire list`` — NF-357 published indicators that fired.
- ``nova safety signal list``   — NF-354/355/356/358 external alignment-risk
  signals (deception, sandbagging, autonomy attempts, elicitation).

**Group name.** ``nova safety`` is the name the NF-351-360 spec §4.4 fixes, and
it does not collide with the pre-existing ``nova safety-case`` (ADR-0095, a
different top-level command). The C4 guardrail spec (NF-131-140 §4) plans
further ``nova safety …`` subcommands on the same group; :data:`control_app`,
:data:`tripwire_app` and :data:`signal_app` are exported separately so a later slice can mount
them on a shared group without moving this code.

**Exit codes are a contract.** ``0`` means the command read the capsule —
including when it holds *no* decisions or a tripwire *fired*: reporting a fired
indicator is the command succeeding, and a non-zero exit on it would turn every
reader into a gate nobody declared (ADR-0167 I-1). ``2`` is a usage or input
error (capsule not found, unreadable, or a malformed facet). Every output, JSON
included, carries the in-mission-boundary line (spec §3.16).

**Deviation from spec §4.4.** The spec lists three P3 readers (``signal show``,
``sandbagging show``, ``autonomy list``) and none for NF-358. They are one
``signal list --type …`` here: four near-identical top-level verbs for four
objects of the same shape would be surface for its own sake, and a single
reader answers "what alignment-risk evidence does this run carry" in one call.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.frontier_safety import (
    IN_MISSION_BOUNDARY,
    AutonomyAttempt,
    ControlDecision,
    DeceptionSignal,
    ElicitationRecord,
    FrontierSafetyError,
    FrontierSafetyFacet,
    SandbaggingRecord,
    TripwireTrigger,
    decisions_for_action,
    facet_from_capsule,
    total_attempts,
)

__all__ = ["control_app", "safety_app", "signal_app", "tripwire_app"]

console = Console()
err_console = Console(stderr=True)

_MANIFEST_NAME = "capsule.yaml"

safety_app = typer.Typer(
    help=(
        "Frontier-safety evidence (experimental, ADR-0167): read external "
        "AI-control decisions, fired tripwires and alignment-risk signals "
        "recorded in a capsule. "
        "Record-only — never blocks a workload."
    ),
    no_args_is_help=True,
)
control_app = typer.Typer(
    help="NF-352 AI-control-protocol decision records (external, by reference).",
    no_args_is_help=True,
)
tripwire_app = typer.Typer(
    help="NF-357 tripwire triggers — which published framework indicator fired.",
    no_args_is_help=True,
)
signal_app = typer.Typer(
    help=(
        "NF-354/355/356/358 alignment-risk signals — deception, sandbagging, "
        "autonomy attempts, elicitation (external, by reference)."
    ),
    no_args_is_help=True,
)
safety_app.add_typer(control_app, name="control")
safety_app.add_typer(tripwire_app, name="tripwire")
safety_app.add_typer(signal_app, name="signal")

CapsuleOpt = Annotated[
    str,
    typer.Option(
        "--capsule",
        help="Capsule directory, or a bare run id resolved under the capsule dir.",
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


# ── Loading ───────────────────────────────────────────────────────────────


def _fail(message: str) -> typer.Exit:
    """Print the boundary line and an input error, and return exit code 2."""
    err_console.print(IN_MISSION_BOUNDARY, markup=False, highlight=False)
    err_console.print(f"[red]{message}[/red]")
    return typer.Exit(2)


def _load_facet(capsule: str) -> tuple[Path, FrontierSafetyFacet | None]:
    """Resolve ``capsule`` and read its frontier-safety facet (None if absent).

    Raises:
        typer.Exit: code 2 when the capsule cannot be found or read, or its
            facet does not validate. Absence of a facet is *not* an error.
    """
    try:
        capsule_dir = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        raise _fail(str(exc)) from exc
    try:
        manifest = yaml.safe_load((capsule_dir / _MANIFEST_NAME).read_text("utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise _fail(f"Could not read {_MANIFEST_NAME}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise _fail(f"{_MANIFEST_NAME} is not a mapping.")
    try:
        return capsule_dir, facet_from_capsule(manifest)
    except (FrontierSafetyError, ValidationError) as exc:
        raise _fail(
            f"facets.frontier_safety is malformed ({type(exc).__name__}); "
            "the capsule may predate this schema or have been edited."
        ) from exc


def _short(ref: str | None) -> str:
    """Abbreviate a digest for a table cell; full values are in ``--json``."""
    if ref is None:
        return "-"
    return ref if len(ref) <= 23 else f"{ref[:19]}…"


def _emit_json(payload: dict[str, Any]) -> None:
    """Write ``payload`` to stdout as JSON with the boundary line embedded."""
    typer.echo(json.dumps({"boundary": IN_MISSION_BOUNDARY, **payload}, indent=2))


# ── nova safety control show ──────────────────────────────────────────────


def _decision_rows(decisions: tuple[ControlDecision, ...]) -> Table:
    table = Table(show_lines=False)
    for col in ("protocol", "decision", "governed action", "monitor", "verdict_ref", "C4 ref"):
        table.add_column(col)
    for d in decisions:
        table.add_row(
            d.protocol,
            d.decision,
            _short(d.governed_action_ref),
            _short(d.monitor_ref),
            _short(d.verdict_ref),
            _short(d.guardrail_decision_ref),
        )
    return table


@control_app.command("show")
def control_show(
    capsule: CapsuleOpt,
    action: Annotated[
        str | None,
        typer.Option(
            "--action",
            help="Only decisions whose governed_action_ref is this sha256 digest.",
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Show the external AI-control-protocol decisions recorded for a run (NF-352).

    Record-only: lists what an external protocol decided; NovaFabric ran no
    protocol. Exits 0 whenever the capsule was read, even with no decisions.
    """
    capsule_dir, facet = _load_facet(capsule)
    decisions = facet.all_control_decisions() if facet else ()
    if action is not None:
        decisions = decisions_for_action(decisions, action)
    if as_json:
        _emit_json(
            {
                "capsule": str(capsule_dir),
                "control_decisions": [
                    d.model_dump(mode="json", exclude_none=True) for d in decisions
                ],
            }
        )
        return
    console.print(IN_MISSION_BOUNDARY, markup=False, highlight=False)
    if not decisions:
        console.print(
            "No control-protocol decisions recorded (not evaluated — this is "
            "neither safe nor unsafe)."
        )
        return
    console.print(f"{len(decisions)} control-protocol decision(s) in {capsule_dir}")
    console.print(_decision_rows(decisions))


# ── nova safety tripwire list ─────────────────────────────────────────────


def _trigger_rows(triggers: tuple[TripwireTrigger, ...]) -> Table:
    table = Table(show_lines=False)
    for col in ("indicator", "framework", "commitment", "triggered by", "verdict_ref"):
        table.add_column(col)
    for t in triggers:
        table.add_row(
            t.indicator_id,
            t.framework,
            _short(t.commitment_ref),
            _short(t.triggered_by_ref),
            _short(t.verdict_ref),
        )
    return table


@tripwire_app.command("list")
def tripwire_list(
    capsule: CapsuleOpt,
    framework: Annotated[
        str | None,
        typer.Option(
            "--framework",
            help="Only triggers for this framework (anthropic_rsp, "
            "openai_preparedness, deepmind_fsf, other).",
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """List the published framework indicators that fired on a run (NF-357).

    Record-only: a fired tripwire is reported, never acted on — no safeguard
    is applied and the command exits 0 when triggers are present.
    """
    capsule_dir, facet = _load_facet(capsule)
    triggers = facet.all_tripwire_triggers() if facet else ()
    if framework is not None:
        triggers = tuple(t for t in triggers if t.framework == framework)
    if as_json:
        _emit_json(
            {
                "capsule": str(capsule_dir),
                "tripwire_triggers": [
                    t.model_dump(mode="json", exclude_none=True) for t in triggers
                ],
            }
        )
        return
    console.print(IN_MISSION_BOUNDARY, markup=False, highlight=False)
    if not triggers:
        console.print("No tripwire triggers recorded.")
        return
    console.print(f"{len(triggers)} tripwire trigger(s) fired in {capsule_dir}")
    console.print(_trigger_rows(triggers))


# ── nova safety signal list ───────────────────────────────────────────────


class SignalKind(str, Enum):
    """The ``--type`` filter of ``nova safety signal list`` (one per P3 object)."""

    deception = "deception"
    sandbagging = "sandbagging"
    autonomy = "autonomy"
    elicitation = "elicitation"


#: JSON key for each kind. Order is the output order, so output is stable.
_SIGNAL_KEYS: dict[SignalKind, str] = {
    SignalKind.deception: "deception_signals",
    SignalKind.sandbagging: "sandbagging_records",
    SignalKind.autonomy: "autonomy_attempts",
    SignalKind.elicitation: "elicitation_records",
}


def _signal_records(
    facet: FrontierSafetyFacet | None,
) -> dict[SignalKind, tuple[Any, ...]]:
    """Every P3 record in ``facet``, keyed by kind (empty tuples when absent)."""
    facet = facet or FrontierSafetyFacet()
    return {
        SignalKind.deception: facet.all_deception_signals(),
        SignalKind.sandbagging: facet.all_sandbagging_records(),
        SignalKind.autonomy: facet.all_autonomy_attempts(),
        SignalKind.elicitation: facet.all_elicitation_records(),
    }


def _deception_table(rows: tuple[DeceptionSignal, ...]) -> Table:
    table = Table(show_lines=False)
    for col in ("signal type", "detector", "evidence", "verdict_ref"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.signal_type, _short(r.detector_ref), _short(r.evidence_digest), _short(r.verdict_ref)
        )
    return table


def _sandbagging_table(rows: tuple[SandbaggingRecord, ...]) -> Table:
    table = Table(show_lines=False)
    for col in ("regime", "observed vs declared", "declared ceiling", "source", "verdict_ref"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.elicitation_regime,
            r.observed_vs_declared,
            _short(r.declared_ceiling_ref),
            r.verdict_source or "-",
            _short(r.verdict_ref),
        )
    return table


def _autonomy_table(rows: tuple[AutonomyAttempt, ...]) -> Table:
    table = Table(show_lines=False)
    for col in ("attempt type", "outcome (reported)", "count", "report", "verdict_ref"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r.attempt_type,
            r.outcome,
            str(r.attempt_count),
            _short(r.report_ref),
            _short(r.verdict_ref),
        )
    return table


def _elicitation_table(rows: tuple[ElicitationRecord, ...]) -> Table:
    table = Table(show_lines=False)
    for col in ("effort (reported)", "elicitation report", "no ceiling computed"):
        table.add_column(col)
    for r in rows:
        table.add_row(r.elicitation_effort, _short(r.elicitation_ref), "true")
    return table


_SIGNAL_LABELS: dict[SignalKind, str] = {
    SignalKind.deception: "deception signal(s) (NF-354)",
    SignalKind.sandbagging: "sandbagging record(s) (NF-355)",
    SignalKind.autonomy: "autonomy attempt record(s) (NF-356)",
    SignalKind.elicitation: "elicitation record(s) (NF-358)",
}


def _signal_table(kind: SignalKind, rows: tuple[Any, ...]) -> Table:
    """Render ``rows`` of one kind as a table."""
    if kind is SignalKind.deception:
        return _deception_table(rows)
    if kind is SignalKind.sandbagging:
        return _sandbagging_table(rows)
    if kind is SignalKind.autonomy:
        return _autonomy_table(rows)
    return _elicitation_table(rows)


@signal_app.command("list")
def signal_list(
    capsule: CapsuleOpt,
    kind: Annotated[
        SignalKind | None,
        typer.Option(
            "--type",
            help="Only this kind: deception (NF-354), sandbagging (NF-355), "
            "autonomy (NF-356) or elicitation (NF-358).",
            case_sensitive=False,
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """List the external alignment-risk signals recorded for a run (NF-354/355/356/358).

    Record-only: every signal, comparison and outcome shown is an external
    evaluator's, held by reference. NovaFabric ran no detector, no sandbox-escape
    test and no elicitation, and computed no ceiling. Exits 0 whenever the
    capsule was read — a reported sandbox escape is reported, never a gate.
    """
    capsule_dir, facet = _load_facet(capsule)
    records = _signal_records(facet)
    kinds = [kind] if kind is not None else list(SignalKind)
    if as_json:
        payload: dict[str, Any] = {"capsule": str(capsule_dir), "types": [k.value for k in kinds]}
        for k in kinds:
            payload[_SIGNAL_KEYS[k]] = [
                r.model_dump(mode="json", exclude_none=True) for r in records[k]
            ]
        if SignalKind.autonomy in kinds:
            payload["total_attempt_count"] = total_attempts(records[SignalKind.autonomy])
        _emit_json(payload)
        return
    console.print(IN_MISSION_BOUNDARY, markup=False, highlight=False)
    if not any(records[k] for k in kinds):
        console.print(
            "No alignment-risk signals recorded (not evaluated — this is neither safe nor unsafe)."
        )
        return
    for k in kinds:
        rows = records[k]
        if not rows:
            console.print(f"No {_SIGNAL_LABELS[k]} recorded.")
            continue
        console.print(f"{len(rows)} {_SIGNAL_LABELS[k]} in {capsule_dir}")
        console.print(_signal_table(k, rows))
    if SignalKind.autonomy in kinds and records[SignalKind.autonomy]:
        console.print(
            f"Reported attempts in total: {total_attempts(records[SignalKind.autonomy])} "
            "(a count from external reports — not a risk score)."
        )
