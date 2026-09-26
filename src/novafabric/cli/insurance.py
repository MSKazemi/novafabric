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

"""``nova insurance`` — risk-transfer evidence (ADR-0170, experimental).

P2 ships three sub-commands named by ADR-0170 / spec §4.4:

- ``liability show``  — NF-383 liability-attribution chain (evidence, not fault);
- ``sla verify``      — NF-384 observed vs declared SLA/warranty threshold;
- ``coverage check``  — NF-386 exclusion-aware trigger facts (facts, not coverage).

The other ``insurance`` sub-commands the ADR lists (``features``, ``loss
bind``, ``subrogation``, ``insurability``) are not implemented yet.

Every output — rich or ``--json`` — carries the in-mission-boundary line:
evidence supports, never determines, an insurance or legal outcome. Nothing
here decides fault, coverage, a claim, or a payout.

Read-only by default. ``--write`` persists the recorded object into the
capsule's ``capsule.yaml`` under ``facets.risk_transfer`` (validated as a
whole facet first); without it nothing on disk changes. The rewrite is atomic;
a NovaSeal-sealed capsule is refused unless ``--force-unseal`` is passed (the
seal then no longer verifies and must be re-issued), and a symlinked
``capsule.yaml`` or capsule directory is always refused.

**Exit codes.** ``0`` the comparison/chain was recorded (a breach or a matched
exclusion is a *recorded fact*, not a failure). ``2`` usage or input error:
no such capsule, unreadable/oversize/malformed input, a float or non-finite
figure, a non-digest reference, or a determination-shaped field.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from novafabric.capsule._manifest_write import FORCE_UNSEAL_FLAG, FORCE_UNSEAL_HELP

app = typer.Typer(
    name="insurance",
    help=(
        "Risk-transfer evidence: liability chain, SLA breach, coverage trigger "
        "(experimental, ADR-0170 NF-383/384/386). Records evidence — never decides "
        "fault, coverage, a claim or a payout."
    ),
    no_args_is_help=True,
)
liability_app = typer.Typer(
    name="liability",
    help="NF-383 liability-attribution chain — attribution evidence, never fault.",
    no_args_is_help=True,
)
sla_app = typer.Typer(
    name="sla",
    help="NF-384 SLA/warranty breach — observed vs declared threshold, no remedy.",
    no_args_is_help=True,
)
coverage_app = typer.Typer(
    name="coverage",
    help="NF-386 coverage trigger — exclusion-aware facts, never a coverage decision.",
    no_args_is_help=True,
)
app.add_typer(liability_app, name="liability")
app.add_typer(sla_app, name="sla")
app.add_typer(coverage_app, name="coverage")

console = Console()
err_console = Console(stderr=True)

IN_MISSION_BOUNDARY = (
    "Evidence supports, never determines, an insurance or legal outcome — "
    "NovaFabric does not assign fault, decide coverage, adjudicate a claim or pay out "
    "(ADR-0170)."
)

_MANIFEST_NAME = "capsule.yaml"
#: Cap on a declared input document (chain, SLA terms, exclusion set, facts).
MAX_INPUT_BYTES = 1 * 1024 * 1024
#: Cap on a capsule manifest read.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class _InputError(Exception):
    """A user-facing input problem; printed and mapped to exit 2."""


# ── helpers ───────────────────────────────────────────────────────────────


def _fail(message: str) -> typer.Exit:
    err_console.print(f"[red]{message}[/red]", soft_wrap=True)
    return typer.Exit(2)


def _capsule_dir(ref: str) -> Path:
    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref

    try:
        return resolve_capsule_ref(ref)
    except CapsuleRefError as exc:
        raise _fail(str(exc)) from exc


def _read_bounded(path: Path, cap: int) -> bytes:
    try:
        size = path.stat().st_size
        if size > cap:
            raise _InputError(f"{path.name} is {size} bytes (cap {cap})")
        with path.open("rb") as fh:
            data = fh.read(cap + 1)
    except OSError as exc:
        raise _InputError(f"could not read {path}: {exc}") from exc
    if len(data) > cap:
        raise _InputError(f"{path.name} exceeds {cap} bytes")
    return data


def _read_json(path: Path) -> tuple[Any, bytes]:
    """Return ``(parsed, raw_bytes)``; JSON numbers parse as exact Decimal."""
    raw = _read_bounded(path, MAX_INPUT_BYTES)
    try:
        return json.loads(raw, parse_float=Decimal), raw
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _InputError(f"{path.name} is not valid JSON: {exc}") from exc


def _read_manifest(capsule_dir: Path) -> dict[str, Any]:
    raw = _read_bounded(capsule_dir / _MANIFEST_NAME, MAX_MANIFEST_BYTES)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise _InputError(f"could not parse {_MANIFEST_NAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise _InputError(f"{_MANIFEST_NAME} is not a mapping")
    return data


def _existing_facet(manifest: dict[str, Any]) -> dict[str, Any]:
    facets = manifest.get("facets")
    block = facets.get("risk_transfer") if isinstance(facets, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def _write(
    capsule_dir: Path, manifest: dict[str, Any], key: str, value: Any, *, force_unseal: bool
) -> None:
    """Merge ``key`` into ``facets.risk_transfer`` after whole-facet validation.

    The manifest is replaced atomically; a sealed capsule is refused (exit 2)
    unless ``force_unseal``, and a symlinked manifest/capsule dir always is.
    """
    from novafabric.capsule._manifest_write import (
        UNSEAL_WARNING,
        ManifestWriteError,
        write_capsule_manifest,
    )
    from novafabric.risk_transfer import RiskTransferFacet, attach_facet

    merged = _existing_facet(manifest)
    merged[key] = value
    facet = RiskTransferFacet.model_validate(merged)
    updated = attach_facet(manifest, facet)
    text = yaml.safe_dump(updated, sort_keys=False)
    try:
        result = write_capsule_manifest(capsule_dir, text, force_unseal=force_unseal)
    except ManifestWriteError as exc:
        raise _fail(str(exc)) from exc
    if result.was_sealed:
        err_console.print(f"[bold red]{UNSEAL_WARNING}[/bold red]", soft_wrap=True)


def _emit_json(key: str, body: Any) -> None:
    print(json.dumps({key: body, "in_mission_boundary": IN_MISSION_BOUNDARY}, indent=2))


def _boundary() -> None:
    console.print(f"[dim]{IN_MISSION_BOUNDARY}[/dim]", soft_wrap=True)


def _input_errors() -> tuple[type[BaseException], ...]:
    from pydantic import ValidationError

    from novafabric.risk_transfer import (
        DeterminationFieldRejectedError,
        InconsistentComparisonError,
        InvalidDecimalError,
        InvalidLiabilityChainError,
        InvalidReferenceError,
        PaymentSecretRejectedError,
        UnboundedFieldError,
        UnsourcedContributionError,
    )

    return (
        _InputError,
        ValidationError,
        ValueError,
        TypeError,
        DeterminationFieldRejectedError,
        InconsistentComparisonError,
        InvalidDecimalError,
        InvalidLiabilityChainError,
        InvalidReferenceError,
        PaymentSecretRejectedError,
        UnboundedFieldError,
        UnsourcedContributionError,
    )


# ── liability show (NF-383) ───────────────────────────────────────────────


@liability_app.command("show")
def liability_show(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    chain: Annotated[
        Path | None,
        typer.Option(
            "--chain",
            help="Declared chain JSON: a list of {role, party_ref, acted_as_ref, basis_ref, "
            "contribution_marker} (or {liability_chain: [...]}). Default: read the capsule's "
            "facets.risk_transfer.liability_chain.",
        ),
    ] = None,
    write: Annotated[
        bool, typer.Option("--write", help="Persist the --chain into capsule.yaml.")
    ] = False,
    force_unseal: Annotated[bool, typer.Option(FORCE_UNSEAL_FLAG, help=FORCE_UNSEAL_HELP)] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the liability-attribution chain (NF-383) — who was attributed, never who is at fault.

    \b
    Examples:
      nova insurance liability show --capsule run_1
      nova insurance liability show --capsule run_1 --chain chain.json --write
    """
    from novafabric.risk_transfer import build_liability_chain

    capsule_dir = _capsule_dir(capsule)
    try:
        if write and chain is None:
            raise _InputError("--write requires --chain")
        manifest = _read_manifest(capsule_dir)
        if chain is not None:
            doc, _raw = _read_json(chain)
            entries = doc.get("liability_chain") if isinstance(doc, dict) else doc
        else:
            entries = _existing_facet(manifest).get("liability_chain")
        if entries is not None and not isinstance(entries, list):
            raise _InputError("liability_chain must be a JSON list")
        edges = build_liability_chain(entries or [])
        body = None if edges is None else [e.model_dump(mode="json") for e in edges]
        if write and body is not None:
            _write(capsule_dir, manifest, "liability_chain", body, force_unseal=force_unseal)
    except _input_errors() as exc:
        raise _fail(f"Invalid liability chain: {exc}") from exc

    if json_out:
        _emit_json("liability_chain", body)
        raise typer.Exit(0)
    if body is None:
        console.print(
            "No liability chain recorded (absent means not recorded — not that no party "
            "contributed)."
        )
    else:
        table = Table(title="Liability-attribution chain (evidence, not a fault finding)")
        for col in ("#", "role", "party_ref", "acted_as_ref", "basis_ref", "contribution"):
            table.add_column(col)
        for i, edge in enumerate(body):
            table.add_row(
                str(i),
                edge["role"],
                _short(edge["party_ref"]),
                _short(edge.get("acted_as_ref")),
                _short(edge.get("basis_ref")),
                edge["contribution_marker"],
            )
        console.print(table)
        if write:
            console.print("[green]Wrote[/green] facets.risk_transfer.liability_chain")
    _boundary()


def _short(ref: str | None) -> str:
    return "-" if not ref else f"{ref[:19]}…"


# ── sla verify (NF-384) ───────────────────────────────────────────────────


@sla_app.command("verify")
def sla_verify(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    sla: Annotated[
        Path,
        typer.Option(
            "--sla",
            help="Declared SLA/warranty terms JSON: {metric, operator (lt|lte|gt|gte|eq), "
            "threshold, window, unit?}. Its sha256 becomes sla_ref.",
        ),
    ],
    observed: Annotated[
        str, typer.Option("--observed", help="Observed value, as an exact decimal string.")
    ],
    evidence_ref: Annotated[
        list[str] | None,
        typer.Option("--evidence-ref", help="sha256:<hex> evidencing the observation; repeat."),
    ] = None,
    write: Annotated[
        bool, typer.Option("--write", help="Persist sla_breach into capsule.yaml.")
    ] = False,
    force_unseal: Annotated[bool, typer.Option(FORCE_UNSEAL_FLAG, help=FORCE_UNSEAL_HELP)] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Compare an observed value to a declared SLA/warranty threshold (NF-384).

    Records ``breach`` (the declared commitment did not hold) with both
    figures. Numeric condition only — no remedy, service credit or damages.

    \b
    Examples:
      nova insurance sla verify --capsule run_1 --sla sla.json --observed 99.2
      nova insurance sla verify --capsule run_1 --sla sla.json --observed 99.2 --json
    """
    from novafabric.risk_transfer import build_sla_breach, digest_artifact

    capsule_dir = _capsule_dir(capsule)
    try:
        manifest = _read_manifest(capsule_dir)
        terms, raw = _read_json(sla)
        if not isinstance(terms, dict):
            raise _InputError("--sla must be a JSON object")
        record = build_sla_breach(
            sla_ref=digest_artifact(raw),
            metric=terms.get("metric", ""),
            operator=terms.get("operator", ""),
            threshold=terms.get("threshold"),
            observed_value=observed,
            window=terms.get("window", ""),
            unit=terms.get("unit"),
            evidence_refs=evidence_ref or [],
        )
        body = record.model_dump(mode="json", exclude_none=True)
        if write:
            _write(capsule_dir, manifest, "sla_breach", body, force_unseal=force_unseal)
    except _input_errors() as exc:
        raise _fail(f"Invalid SLA input: {exc}") from exc

    if json_out:
        _emit_json("sla_breach", body)
        raise typer.Exit(0)
    unit = f" {body['unit']}" if body.get("unit") else ""
    console.print(f"sla_ref: {body['sla_ref']}")
    console.print(
        f"metric: {body['metric']}  declared: {body['operator']} {body['threshold']}{unit}  "
        f"observed: {body['observed_value']}{unit}  window: {body['window']}"
    )
    console.print(
        f"breach: {str(body['breach']).lower()} "
        "[dim](numeric condition only — no remedy, credit or damages assessed)[/dim]"
    )
    if write:
        console.print("[green]Wrote[/green] facets.risk_transfer.sla_breach")
    _boundary()


# ── coverage check (NF-386) ───────────────────────────────────────────────


@coverage_app.command("check")
def coverage_check(
    capsule: Annotated[str, typer.Option("--capsule", help="Capsule directory or run id.")],
    exclusions: Annotated[
        Path,
        typer.Option(
            "--exclusions",
            help="Declared exclusion set JSON: {exclusions: [{exclusion_id, fact_markers[]}], "
            "parametric_triggers?: [{metric, operator, threshold, unit?}]}. Its sha256 "
            "becomes exclusion_set_ref.",
        ),
    ],
    facts: Annotated[
        Path,
        typer.Option(
            "--facts",
            help="Observed facts JSON: {trigger_facts: [{fact_ref, marker}], "
            "observations?: {metric: value}}.",
        ),
    ],
    event_kind: Annotated[
        str, typer.Option("--event-kind", help="covered_event_kind label, e.g. erroneous_output.")
    ],
    write: Annotated[
        bool, typer.Option("--write", help="Persist coverage_trigger into capsule.yaml.")
    ] = False,
    force_unseal: Annotated[bool, typer.Option(FORCE_UNSEAL_FLAG, help=FORCE_UNSEAL_HELP)] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Record exclusion-aware coverage-trigger facts (NF-386) — never whether the policy responds.

    \b
    Examples:
      nova insurance coverage check --capsule run_1 --exclusions cg4047.json \\
          --facts facts.json --event-kind erroneous_output
    """
    from novafabric.risk_transfer import build_coverage_trigger, digest_artifact

    capsule_dir = _capsule_dir(capsule)
    try:
        manifest = _read_manifest(capsule_dir)
        declared, raw = _read_json(exclusions)
        observed, _ = _read_json(facts)
        if not isinstance(declared, dict) or not isinstance(observed, dict):
            raise _InputError("--exclusions and --facts must be JSON objects")
        observations = observed.get("observations") or {}
        if not isinstance(observations, dict):
            raise _InputError("observations must be a JSON object")
        record = build_coverage_trigger(
            covered_event_kind=event_kind,
            exclusion_set_ref=digest_artifact(raw),
            trigger_facts=_as_list(observed.get("trigger_facts"), "trigger_facts"),
            declared_exclusions=_as_list(declared.get("exclusions"), "exclusions"),
            parametric_terms=_as_list(declared.get("parametric_triggers"), "parametric_triggers"),
            observations=observations,
        )
        body = record.model_dump(mode="json", exclude_none=True)
        if write:
            _write(capsule_dir, manifest, "coverage_trigger", body, force_unseal=force_unseal)
    except _input_errors() as exc:
        raise _fail(f"Invalid coverage input: {exc}") from exc

    if json_out:
        _emit_json("coverage_trigger", body)
        raise typer.Exit(0)
    console.print(f"covered_event_kind: {body['covered_event_kind']}")
    console.print(f"exclusion_set_ref: {body['exclusion_set_ref']}")
    console.print(f"trigger_facts: {len(body['trigger_facts'])}")
    matched = body["matched_exclusions"]
    if matched:
        for m in matched:
            console.print(
                f"matched exclusion facts: {m['exclusion_id']} "
                f"(markers: {', '.join(m['matched_markers'])}; {len(m['fact_refs'])} fact(s))"
            )
    else:
        console.print(
            "matched_exclusions: [] [dim](no declared exclusion's facts present — this "
            "does not mean the policy responds)[/dim]"
        )
    for cond in body.get("parametric_conditions", []):
        met = cond.get("condition_met")
        state = "not observed" if met is None else f"condition_met={str(met).lower()}"
        console.print(
            f"parametric: {cond['metric']} {cond['operator']} {cond['threshold']} → {state}"
        )
    if write:
        console.print("[green]Wrote[/green] facets.risk_transfer.coverage_trigger")
    _boundary()


def _as_list(value: Any, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _InputError(f"{name} must be a JSON list")
    return value


__all__ = ["IN_MISSION_BOUNDARY", "app"]
