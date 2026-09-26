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

Experimental. P2 ships ``odd show`` (NF-303) and ``trajectory verify``
(NF-310); P3 adds ``sim2real show`` / ``sim2real verify`` (NF-304),
``teleop list`` (NF-305) and ``timing show`` (NF-308). All read the capsule
manifest only (plus, for ``sim2real verify``, local artifact files the user
names) — no robot, network, or control-plane contact — and every output
carries the in-mission-boundary line: NovaFabric records; it never actuates
or adjudicates.

**Exit codes are a contract.**

- ``0`` — the command did its job: a record was shown (including "none
  recorded"), or a chain/binding was walked and nothing contradicts it
  (warnings such as an ``unbound`` sim policy never fail).
- ``1`` — the recorded evidence is defective: a trajectory with a broken,
  cyclic or out-of-order link; an ODD block that carries a ruling
  (non-null ``verdict``, ``in_odd: true``); a sim2real binding contradicted
  by a local artifact or naming a different deployment run; or any block with
  a raw payload, a non-digest ref, a PII-shaped operator, out-of-order
  timestamps, or a duplicate clock domain.
- ``2`` — nothing could be checked: the capsule was not found or not readable
  (or over the manifest size cap), a named local artifact could not be read,
  or (``trajectory verify`` / ``sim2real verify`` only) nothing is recorded. A verifier that
  exits ``0`` on an absent chain would pass exactly the capsules with nothing
  to check.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

embodied_app = typer.Typer(
    name="embodied",
    help=(
        "Embodied/cyber-physical evidence: ODD record, trajectory chain, sim2real "
        "lineage, teleop handoffs and clock timing, offline (experimental, "
        "ADR-0162). Record-only — never actuates or adjudicates."
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
sim2real_app = typer.Typer(
    name="sim2real",
    help="NF-304 sim-trained policy → real deployment lineage (unbound is recorded).",
    no_args_is_help=True,
)
teleop_app = typer.Typer(
    name="teleop",
    help="NF-305 autonomy↔human handoffs (pseudonymous operator refs).",
    no_args_is_help=True,
)
timing_app = typer.Typer(
    name="timing",
    help="NF-308 per-clock-domain offset + latency evidence (no clock disciplined).",
    no_args_is_help=True,
)
embodied_app.add_typer(odd_app, name="odd")
embodied_app.add_typer(trajectory_app, name="trajectory")
embodied_app.add_typer(sim2real_app, name="sim2real")
embodied_app.add_typer(teleop_app, name="teleop")
embodied_app.add_typer(timing_app, name="timing")

console = Console()
err_console = Console(stderr=True)

_MANIFEST_NAME = "capsule.yaml"
#: A capsule manifest is metadata; anything larger is refused before parsing
#: so a hostile capsule cannot turn a read-only ``show`` into a memory bomb.
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024

CapsuleOpt = Annotated[
    str,
    typer.Option(
        "--capsule",
        help="Capsule directory, or a bare run id under the capsule store.",
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _load_manifest(capsule: str) -> dict[str, Any]:
    """Return the parsed capsule manifest; exit ``2`` when it cannot be read."""
    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref

    try:
        capsule_dir = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(2) from exc
    path = capsule_dir / _MANIFEST_NAME
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            err_console.print(
                f"[red]{_MANIFEST_NAME} exceeds the {_MAX_MANIFEST_BYTES}-byte cap.[/red]"
            )
            raise typer.Exit(2)
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        err_console.print(f"[red]Could not read {_MANIFEST_NAME}:[/red] {escape(str(exc))}")
        raise typer.Exit(2) from exc
    if not isinstance(manifest, dict):
        err_console.print(f"[red]{_MANIFEST_NAME} is not a mapping.[/red]")
        raise typer.Exit(2)
    return manifest


def _embodied_of(manifest: dict[str, Any]) -> dict[str, Any] | None:
    facets = manifest.get("facets")
    embodied = facets.get("embodied") if isinstance(facets, dict) else None
    return embodied if isinstance(embodied, dict) else None


def _load_embodied(capsule: str) -> dict[str, Any] | None:
    """Return the raw ``facets.embodied`` mapping, or ``None`` when absent.

    Exits ``2`` when the capsule cannot be found or read. Raw rather than
    validated so each subcommand validates only the object it reports on: a
    defect in one embodied object must not hide the evidence in another.
    """
    return _embodied_of(_load_manifest(capsule))


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


# ── shared P3 helpers ─────────────────────────────────────────────────────

#: Bound on NF-202 checkpoint digests read for the sim2real cross-check.
_MAX_CHECKPOINTS = 10_000


def _not_recorded(json_out: bool, what: str, why: str, *, verify: bool = False) -> None:
    """Report an absent object: exit 0 for ``show``/``list``, 2 for ``verify``."""
    if json_out:
        payload: dict[str, Any] = {"recorded": False, "boundary": _boundary()}
        if verify:
            payload["ok"] = False
        _emit_json(payload)
    else:
        printer = err_console if verify else console
        printer.print(f"No {what} recorded in this capsule — {why}")
        console.print(_boundary())
    if verify:
        raise typer.Exit(2)


def _p3_errors() -> tuple[type[Exception], ...]:
    """Every named error a P3 object's validation can raise."""
    from novafabric.embodied import (
        ClockDomainConflictError,
        HandoffOrderError,
        InvalidClockDomainError,
        InvalidDeploymentRunError,
        InvalidLatencyError,
        InvalidReferenceError,
        InvalidTimestampError,
        InvalidTimingValueError,
        InvalidTriggerError,
        OperatorIdentityError,
        RawPayloadRejectedError,
    )

    return (
        ClockDomainConflictError,
        HandoffOrderError,
        InvalidClockDomainError,
        InvalidDeploymentRunError,
        InvalidLatencyError,
        InvalidReferenceError,
        InvalidTimestampError,
        InvalidTimingValueError,
        InvalidTriggerError,
        OperatorIdentityError,
        RawPayloadRejectedError,
        TypeError,
        ValidationError,
    )


def _load_sim2real(json_out: bool, raw: Any) -> Any:
    from novafabric.embodied import Sim2RealLineage

    try:
        return Sim2RealLineage.model_validate(raw)
    except _p3_errors() as exc:
        _defect(json_out, "sim2real", exc)


def _checkpoint_digests(manifest: dict[str, Any]) -> list[str]:
    """NF-202 ``checkpoint_digest`` values of this capsule, bounded, raw-read."""
    facets = manifest.get("facets")
    provenance = facets.get("model_provenance") if isinstance(facets, dict) else None
    chain = provenance.get("checkpoint_chain") if isinstance(provenance, dict) else None
    if not isinstance(chain, list):
        return []
    return [
        hop["checkpoint_digest"]
        for hop in chain[:_MAX_CHECKPOINTS]
        if isinstance(hop, dict) and isinstance(hop.get("checkpoint_digest"), str)
    ]


# ── nova embodied sim2real show ───────────────────────────────────────────


@sim2real_app.command("show")
def sim2real_show_cmd(capsule: CapsuleOpt, json_out: JsonOpt = False) -> None:
    """Show the sim-trained policy → real deployment binding (NF-304).

    Exits 1 if the recorded block is malformed (non-digest ref, bad run id,
    raw payload); 2 if the capsule cannot be read. None recorded is reported,
    not failed. ``unbound: true`` is shown as recorded — never an error.

    \b
    Examples:
      nova embodied sim2real show --capsule ./capsules/run-01HX
      nova embodied sim2real show --capsule 01HXAY7M5JZ8R7K4P9DPBYK2WX --json
    """
    embodied = _load_embodied(capsule)
    raw = None if embodied is None else embodied.get("sim2real")
    if raw is None:
        _not_recorded(
            json_out, "sim2real binding", "which says nothing about where the policy came from."
        )
        return
    lineage = _load_sim2real(json_out, raw)
    if json_out:
        _emit_json(
            {
                "recorded": True,
                "sim2real": lineage.model_dump(exclude_none=True),
                "boundary": _boundary(),
            }
        )
        return
    console.print(f"sim_policy_ref:    {lineage.sim_policy_ref or '(none recorded)'}")
    console.print(f"sim_env_ref:       {lineage.sim_env_ref}")
    console.print(f"randomization_ref: {lineage.randomization_ref or '(none declared)'}")
    console.print(f"deployment_run_id: {escape(lineage.deployment_run_id)}")
    console.print(f"unbound:           {str(lineage.unbound).lower()}")
    if lineage.unbound:
        console.print(
            "  the policy's simulation origin is recorded but not pinned — recorded, not fatal"
        )
    console.print(_boundary())


# ── nova embodied sim2real verify ─────────────────────────────────────────


@sim2real_app.command("verify")
def sim2real_verify_cmd(
    capsule: CapsuleOpt,
    policy: Annotated[
        Path | None,
        typer.Option(
            "--policy", dir_okay=False, help="Local policy checkpoint to re-hash vs sim_policy_ref."
        ),
    ] = None,
    env: Annotated[
        Path | None,
        typer.Option(
            "--env", dir_okay=False, help="Local sim/world config to re-hash vs sim_env_ref."
        ),
    ] = None,
    randomization: Annotated[
        Path | None,
        typer.Option(
            "--randomization",
            dir_okay=False,
            help="Local domain-randomization spec to re-hash vs randomization_ref.",
        ),
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """Walk the sim→real binding offline (NF-304).

    Checks that ``deployment_run_id`` names this capsule, whether
    ``sim_policy_ref`` appears in this capsule's NF-202 checkpoint chain, and —
    for each local file given — that it hashes to the recorded digest. Exits 0
    when nothing contradicts the record (an ``unbound`` policy is a warning:
    recorded, not fatal), 1 on a digest or deployment mismatch or a malformed
    block, 2 when nothing is recorded or a capsule/file cannot be read.

    \b
    Examples:
      nova embodied sim2real verify --capsule ./capsules/run-01HX
      nova embodied sim2real verify --capsule 01HXAY --policy ./policy.ckpt --json
    """
    from novafabric.embodied import ArtifactReadError, digest_artifact_file, verify_sim2real

    manifest = _load_manifest(capsule)
    embodied = _embodied_of(manifest)
    raw = None if embodied is None else embodied.get("sim2real")
    if raw is None:
        _not_recorded(
            json_out, "sim2real binding", "nothing to verify (absent is not a pass).", verify=True
        )
        return  # pragma: no cover - _not_recorded exits for verify
    lineage = _load_sim2real(json_out, raw)

    supplied = {"sim_policy_ref": policy, "sim_env_ref": env, "randomization_ref": randomization}
    digests: dict[Any, str] = {}
    for field, path in supplied.items():
        if path is None:
            continue
        try:
            digests[field] = digest_artifact_file(path)
        except ArtifactReadError as exc:
            err_console.print(f"[red]Could not hash {field} artifact:[/red] {escape(str(exc))}")
            raise typer.Exit(2) from exc

    run_id = manifest.get("run_id")
    report = verify_sim2real(
        lineage,
        capsule_run_id=run_id if isinstance(run_id, str) else None,
        checkpoint_digests=_checkpoint_digests(manifest),
        artifact_digests=digests,
    )
    if json_out:
        _emit_json(
            {"recorded": True, "ok": report.ok, **report.model_dump(), "boundary": _boundary()}
        )
    else:
        console.print(f"sim2real: {'OK' if report.ok else 'CONTRADICTED'}")
        console.print(
            f"  unbound: {str(report.unbound).lower()}  "
            f"deployment_matches: {report.deployment_matches}  "
            f"checkpoint_chain_bound: {str(report.checkpoint_chain_bound).lower()}"
        )
        for finding in report.findings:
            console.print(f"  {finding.severity.upper()} {finding.code}: {escape(finding.message)}")
        console.print(
            "Walked recorded digests only; no simulation was re-run and no policy was loaded."
        )
        console.print(_boundary())
    if not report.ok:
        raise typer.Exit(1)


# ── nova embodied teleop list ─────────────────────────────────────────────


@teleop_app.command("list")
def teleop_list_cmd(
    capsule: CapsuleOpt,
    direction: Annotated[
        str | None,
        typer.Option(
            "--direction",
            help="Only show handoffs in this direction (autonomy_to_human | human_to_autonomy).",
        ),
    ] = None,
    json_out: JsonOpt = False,
) -> None:
    """List recorded autonomy↔human handoffs (NF-305). Operators are pseudonymous.

    Exits 1 if any recorded handoff is malformed (PII-shaped operator ref,
    prose trigger, out-of-range latency, out-of-order ``ts``); 2 if the capsule
    cannot be read or ``--direction`` is not a known direction. None recorded
    is reported, not failed. A repeated direction is a warning, never fatal.

    \b
    Examples:
      nova embodied teleop list --capsule ./capsules/run-01HX
      nova embodied teleop list --capsule 01HXAY --direction autonomy_to_human --json
    """
    from novafabric.embodied import TeleopHandoff, handoff_findings
    from novafabric.embodied.teleop import check_handoff_sequence

    if direction is not None and direction not in ("autonomy_to_human", "human_to_autonomy"):
        err_console.print("[red]--direction must be autonomy_to_human or human_to_autonomy.[/red]")
        raise typer.Exit(2)
    embodied = _load_embodied(capsule)
    raw = None if embodied is None else embodied.get("teleop")
    if raw is None:
        _not_recorded(json_out, "teleop handoff", "which is not a finding that no human took over.")
        return
    try:
        if not isinstance(raw, list):
            raise TypeError("facets.embodied.teleop must be a list of handoffs")
        handoffs = [TeleopHandoff.model_validate(item) for item in raw]
        check_handoff_sequence(handoffs)
    except _p3_errors() as exc:
        _defect(json_out, "teleop", exc)
        return  # pragma: no cover - _defect always exits

    findings = handoff_findings(handoffs)
    shown = [
        (i, h) for i, h in enumerate(handoffs) if direction is None or h.direction == direction
    ]
    if json_out:
        _emit_json(
            {
                "recorded": True,
                "handoff_count": len(handoffs),
                "shown": len(shown),
                "handoffs": [{"index": i, **h.model_dump(exclude_none=True)} for i, h in shown],
                "findings": [f.model_dump() for f in findings],
                "boundary": _boundary(),
            }
        )
        return
    console.print(f"teleop handoffs: {len(handoffs)} recorded, {len(shown)} shown")
    for index, handoff in shown:
        console.print(
            f"  [{index}] {escape(handoff.ts)}  {handoff.direction}  "
            f"operator={escape(handoff.operator_ref)}  trigger={escape(handoff.trigger)}  "
            f"latency_ms={handoff.latency_ms:g}"
        )
    for finding in findings:
        console.print(f"  WARNING {finding.code} (handoff {finding.index}): {finding.message}")
    console.print(
        "Handoffs are recorded, not performed or evaluated; operator refs are pseudonymous."
    )
    console.print(_boundary())


# ── nova embodied timing show ─────────────────────────────────────────────


@timing_app.command("show")
def timing_show_cmd(capsule: CapsuleOpt, json_out: JsonOpt = False) -> None:
    """Show per-clock-domain offset and latency evidence (NF-308).

    Warns for each sensor stream stamped against a clock domain the timing
    block does not describe. Exits 1 if the block is malformed (duplicate
    domain, non-finite or out-of-range value); 2 if the capsule cannot be
    read. None recorded is reported, not failed.

    \b
    Examples:
      nova embodied timing show --capsule ./capsules/run-01HX
      nova embodied timing show --capsule 01HXAY7M5JZ8R7K4P9DPBYK2WX --json
    """
    from novafabric.embodied import ClockTiming, timing_findings
    from novafabric.embodied.timing import check_timing_sequence

    embodied = _load_embodied(capsule)
    raw = None if embodied is None else embodied.get("timing")
    if raw is None:
        _not_recorded(json_out, "timing evidence", "clock offsets and latencies are unrecorded.")
        return
    try:
        if not isinstance(raw, list):
            raise TypeError("facets.embodied.timing must be a list of clock-domain entries")
        entries = [ClockTiming.model_validate(item) for item in raw]
        check_timing_sequence(entries)
    except _p3_errors() as exc:
        _defect(json_out, "timing", exc)
        return  # pragma: no cover - _defect always exits

    sensors = embodied.get("sensors") if embodied is not None else None
    sensor_domains = (
        [s.get("clock_domain") for s in sensors or [] if isinstance(s, dict)]
        if isinstance(sensors, list)
        else []
    )
    findings = timing_findings(entries, [d for d in sensor_domains if isinstance(d, str)])
    if json_out:
        _emit_json(
            {
                "recorded": True,
                "timing": [e.model_dump(exclude_none=True) for e in entries],
                "findings": [f.model_dump() for f in findings],
                "boundary": _boundary(),
            }
        )
        return
    console.print(f"clock domains: {len(entries)}")
    for entry in entries:
        offset = "unrecorded" if entry.offset_ms is None else f"{entry.offset_ms:g}"
        console.print(
            f"  {escape(entry.clock_domain)}  source={entry.source}  offset_ms={offset}  "
            f"max_observed_latency_ms={entry.max_observed_latency_ms:g}"
        )
    for finding in findings:
        console.print(f"  WARNING {finding.code}: {escape(finding.message)}")
    console.print(
        "Timing is declared/observed evidence; NovaFabric synchronized, steered or "
        "corrected no clock."
    )
    console.print(_boundary())


__all__ = ["embodied_app"]
