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
"""Safety-case template renderers (ADR-0095, slice C3).

``render_safety_case(case, fmt)`` is a **pure** function — string in, string out —
that renders a compiled :class:`~novafabric.safetycase.models.SafetyCase` into one of
four surfaces:

- ``annex-iv``  — an EU AI Act Annex IV technical-documentation section. Each claim
  becomes a numbered element carrying its ``backing_state``
  (SUPPORTED/CONTESTED/UNSUPPORTED/UNKNOWN, shown verbatim and **never laundered to
  "compliant"**), its evidence references (artifact ``sha256``), and — for a
  CONTESTED/UNKNOWN claim — the explicit ``contest_reason``.
- ``nist-rmf``  — the same tree grouped by NIST AI RMF function
  (GOVERN / MAP / MEASURE / MANAGE) inferred from claim ids.
- ``markdown``  — a generic readable Markdown document.
- ``json``      — the canonical record (round-trips against the schema).

**Honesty is structural.** A CONTESTED claim *must* render its contest reason; a
``residual_risk`` of ``not-quantified`` renders as such — the renderer never
fabricates a number, and never substitutes a compliance verdict for an honest
backing state.
"""

from __future__ import annotations

import json
from typing import Literal

from novafabric.safetycase.models import (
    BackingState,
    Claim,
    Evidence,
    ResidualRisk,
    SafetyCase,
)

RenderFormat = Literal["markdown", "annex-iv", "nist-rmf", "json"]

#: NIST AI RMF top-level functions, in canonical order.
_NIST_FUNCTIONS: tuple[str, ...] = ("GOVERN", "MAP", "MEASURE", "MANAGE")

_UNGROUPED = "Ungrouped"


def render_safety_case(case: SafetyCase, fmt: RenderFormat) -> str:
    """Render *case* into *fmt*; pure (string in, string out).

    Raises:
        ValueError: if *fmt* is not one of the four known formats.
    """
    if fmt == "json":
        return _render_json(case)
    if fmt == "markdown":
        return _render_markdown(case)
    if fmt == "annex-iv":
        return _render_annex_iv(case)
    if fmt == "nist-rmf":
        return _render_nist_rmf(case)
    raise ValueError(
        f"Unknown render format {fmt!r}; "
        "expected one of: markdown, annex-iv, nist-rmf, json"
    )


# --- shared helpers ----------------------------------------------------------


def _claims(case: SafetyCase) -> list[Claim]:
    return [n for n in case.nodes if isinstance(n, Claim)]


def _evidence_index(case: SafetyCase) -> dict[str, Evidence]:
    return {n.id: n for n in case.nodes if isinstance(n, Evidence)}


def _evidence_lines(claim: Claim, index: dict[str, Evidence]) -> list[str]:
    """Render a claim's bound evidence as `kind → path sha256` lines."""
    lines: list[str] = []
    for ev_id in claim.evidence_ids:
        ev = index.get(ev_id)
        if ev is None:
            lines.append(f"  - evidence: `{ev_id}` (dangling reference)")
            continue
        ref = ev.artifact_ref.path_in_bundle or ev.artifact_ref.external_id or "—"
        lines.append(
            f"  - evidence `{ev.id}` ({ev.evidence_kind.value}): "
            f"`{ref}` `{ev.artifact_ref.sha256}`"
        )
    return lines


def _residual_risk_line(rr: ResidualRisk | None) -> str:
    """Render residual risk honestly: a value only ever appears with a measured basis."""
    if rr is None:
        return "Residual risk: not recorded."
    value = "null" if rr.value is None else str(rr.value)
    caveat = f" — {rr.caveat}" if rr.caveat else ""
    return f"Residual risk: {value} (basis: {rr.basis.value}){caveat}"


def _is_contestable(state: BackingState) -> bool:
    return state in (BackingState.CONTESTED, BackingState.UNKNOWN)


# --- json --------------------------------------------------------------------


def _render_json(case: SafetyCase) -> str:
    return json.dumps(case.to_record(), indent=2)


# --- markdown ----------------------------------------------------------------


def _render_markdown(case: SafetyCase) -> str:
    index = _evidence_index(case)
    lines: list[str] = ["# Safety Case", ""]
    lines.append(f"- **case_id:** `{case.case_id}`")
    lines.append(f"- **template:** `{case.template_id}`")
    lines.append(f"- **created_at:** {case.created_at}")
    lines.append(f"- **subject runs:** {', '.join(case.subject.run_ids)}")
    lines.append(f"- **case_hash:** `{case.case_hash}`")
    lines.append(f"- **{_residual_risk_line(case.residual_risk)}**")
    lines.append("")
    lines.append("## Claims (backing_state)")
    lines.append("")
    for claim in _claims(case):
        lines.append(
            f"- **{claim.id}** [{claim.backing_state.value}] — {claim.statement}"
        )
        if claim.contest_reason:
            lines.append(f"  - contest reason: {claim.contest_reason}")
        lines.extend(_evidence_lines(claim, index))
    return "\n".join(lines)


# --- annex-iv ----------------------------------------------------------------


def _render_annex_iv(case: SafetyCase) -> str:
    """Render the CAE tree as an EU AI Act Annex IV technical-documentation section.

    The top claim is the document subject; each non-root claim becomes a numbered
    element. Backing states are shown verbatim and CONTESTED/UNKNOWN claims surface
    their ``contest_reason``. Nothing is laundered into a "compliant" verdict.
    """
    index = _evidence_index(case)
    claims = _claims(case)
    root = case.get_claim(case.top_claim_id)

    lines: list[str] = ["# EU AI Act — Annex IV Technical Documentation", ""]
    lines.append(f"Template: `{case.template_id}` · case_id: `{case.case_id}`")
    lines.append(f"Subject runs: {', '.join(case.subject.run_ids)}")
    lines.append(f"case_hash: `{case.case_hash}`")
    lines.append("")
    if root is not None:
        lines.append(f"## Subject claim — {root.backing_state.value}")
        lines.append("")
        lines.append(root.statement)
        if root.contest_reason:
            lines.append("")
            lines.append(f"> Contest reason: {root.contest_reason}")
        lines.append("")

    lines.append("## Annex IV elements")
    lines.append("")
    n = 0
    for claim in claims:
        if root is not None and claim.id == root.id:
            continue
        n += 1
        lines.append(
            f"### {n}. {claim.id} — {claim.backing_state.value}"
        )
        lines.append("")
        lines.append(claim.statement)
        if _is_contestable(claim.backing_state) and claim.contest_reason:
            lines.append("")
            lines.append(f"> {claim.backing_state.value}: {claim.contest_reason}")
        ev_lines = _evidence_lines(claim, index)
        if ev_lines:
            lines.append("")
            lines.append("Evidence:")
            lines.extend(ev_lines)
        else:
            lines.append("")
            lines.append("Evidence: none bound (honest hole).")
        lines.append("")

    lines.append("## Residual risk")
    lines.append("")
    lines.append(_residual_risk_line(case.residual_risk))
    return "\n".join(lines).rstrip() + "\n"


# --- nist-rmf ----------------------------------------------------------------


def _nist_function_of(claim_id: str) -> str:
    """Infer the NIST RMF function a claim belongs to from its id, else Ungrouped."""
    upper = claim_id.upper()
    for fn in _NIST_FUNCTIONS:
        if fn in upper:
            return fn
    return _UNGROUPED


def _render_nist_rmf(case: SafetyCase) -> str:
    """Render the CAE tree grouped by NIST AI RMF function (GOVERN/MAP/MEASURE/MANAGE).

    A claim whose id does not encode a function is kept in an ``Ungrouped`` section so
    nothing is silently dropped.
    """
    index = _evidence_index(case)
    root = case.get_claim(case.top_claim_id)

    grouped: dict[str, list[Claim]] = {fn: [] for fn in _NIST_FUNCTIONS}
    grouped[_UNGROUPED] = []
    for claim in _claims(case):
        if root is not None and claim.id == root.id:
            continue
        grouped[_nist_function_of(claim.id)].append(claim)

    lines: list[str] = ["# NIST AI Risk Management Framework", ""]
    lines.append(f"Template: `{case.template_id}` · case_id: `{case.case_id}`")
    lines.append(f"case_hash: `{case.case_hash}`")
    if root is not None:
        lines.append("")
        lines.append(f"Subject claim — {root.backing_state.value}: {root.statement}")
        if root.contest_reason:
            lines.append(f"> Contest reason: {root.contest_reason}")
    lines.append("")

    for fn in (*_NIST_FUNCTIONS, _UNGROUPED):
        claims = grouped[fn]
        if not claims:
            continue
        lines.append(f"## {fn}")
        lines.append("")
        for claim in claims:
            lines.append(
                f"- **{claim.id}** [{claim.backing_state.value}] — {claim.statement}"
            )
            if _is_contestable(claim.backing_state) and claim.contest_reason:
                lines.append(f"  - {claim.backing_state.value}: {claim.contest_reason}")
            lines.extend(_evidence_lines(claim, index))
        lines.append("")

    lines.append("## Residual risk")
    lines.append("")
    lines.append(_residual_risk_line(case.residual_risk))
    return "\n".join(lines).rstrip() + "\n"
