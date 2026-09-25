"""Credit-decision specific-reasons evidence pack (ADR-0159 D6 / NF-278).

A pure **renderer** over facts read from one sealed Run Capsule by :mod:`.adverse_action_collect`.
For a credit-decision run it assembles the evidence a creditor's compliance team uses to author an
ECOA / Reg B adverse-action notice:

* ``model_call`` — the recorded model call(s): model ref, status, and SHA-256 digests of the
  recorded request messages and response choices (never the prompt/response text itself —
  ADR-0021 §4 privacy-by-default);
* ``sealed_inputs`` — the run's ``inputs/`` files, each bound to the capsule's signed
  ``evidence_digests`` map (ADR-0251);
* ``principal_reasons`` — the recorded feature-attribution facet (``facets.feature_attribution``),
  rendered **in the order recorded, with the rank as recorded** — never re-ranked, re-scored,
  recomputed, or paraphrased;
* ``seal`` — whether the capsule carries a NovaSeal DSSE envelope.

Each row is ``complete`` / ``partial`` / ``missing`` with source refs or a machine-readable reason;
a capsule with no attribution facet yields ``principal_reasons: missing`` (spec crosswalk), never
a guessed reason.

Honest limitation: ``feature_attribution`` is **not** yet in the closed run-capsule facet registry
(``schemas/run-capsule.schema.json`` ``properties.facets`` is ``additionalProperties: false``,
ADR-0196 D2). Registering it is a run-capsule schema change that needs its own ADR before any
producer can write it; until then ``principal_reasons`` is always ``missing`` on schema-valid
capsules.

It **renders evidence, never a notice and never a decision**: there is intentionally no notice
text, no verdict/compliant/sufficiency field, and no credit outcome field. The creditor's human
authors, approves, and sends the notice (ADR-0159 D6, spec item 11).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..provenance import EvidenceSource, source_for_status
from .model_independence import FINANCE_HONESTY_BANNER

__all__ = [
    "ADVERSE_ACTION_REGIME",
    "ATTRIBUTION_FACET_KEY",
    "CFPB_HONESTY_LINE",
    "FINANCE_HONESTY_BANNER",
    "SEAL_VERIFY_HINT",
    "AdverseActionPack",
    "AttributionFacts",
    "CapsuleFacts",
    "InputFileFacts",
    "ModelCallFacts",
    "PackRow",
    "ReasonFacts",
    "build_adverse_action_pack",
]

_GAP_STATES = frozenset({"missing"})

RowStatus = Literal["complete", "partial", "missing"]
Number = int | float  # a recorded contribution: int or finite float, never bool or str

#: The single capsule facet this exporter reads as the recorded principal reasons.
ATTRIBUTION_FACET_KEY = "feature_attribution"

#: Version-pinned regime text (ADR-0159 D1 — surface exactly which version is rendered).
ADVERSE_ACTION_REGIME = (
    "ECOA / Reg B 12 CFR 1002.9(a)(2), (b)(2) specific principal reasons; "
    "CFPB Circular 2022-03 (2022-05-26) and 2023-03 (2023-09-19)"
)

#: Mandatory CFPB honesty line (ADR-0159 spec item 11) — carried in every artifact and CLI output.
CFPB_HONESTY_LINE = (
    "This pack is evidence of what the sealed capsule recorded. It is NOT an adverse-action "
    "notice, contains no notice text, decides no credit outcome, and makes no legal "
    "determination. Under ECOA / Reg B the creditor must state the specific principal reasons "
    "for the action taken: 'the model decided', a generic or checklist reason, or the "
    "complexity of a black-box model does not satisfy that duty (CFPB). A human at the creditor "
    "authors, approves, and sends the notice."
)

SEAL_VERIFY_HINT = (
    "seal presence only; re-verify the DSSE signature and evidence digests with `nova verify`"
)

#: Machine-readable reasons (stable strings — downstream tooling may match on them).
REASON_NO_MODEL_CALL = "no model call recorded in model-calls.jsonl"
REASON_MODEL_CALLS_TRUNCATED = "rendered {shown} of {total} recorded model calls (cap reached)"
REASON_MODEL_CALLS_UNBOUND = (
    "model-calls.jsonl is not bound by the capsule's evidence_digests (pre-ADR-0251 capsule)"
)
REASON_NO_INPUTS = "no input file recorded under inputs/"
REASON_INPUTS_UNBOUND = "{n} of {m} input files not bound by the capsule's evidence_digests"
REASON_INPUTS_TRUNCATED = "rendered {shown} of {total} input files (cap reached)"
REASON_NO_ATTRIBUTION = (
    f"no attribution facet (facets.{ATTRIBUTION_FACET_KEY}) recorded in the capsule; "
    "NovaFabric does not compute or infer reasons"
)
REASON_NO_REASONS = f"facets.{ATTRIBUTION_FACET_KEY} is recorded but lists no reasons"
REASON_REASONS_TRUNCATED = "rendered {shown} of {total} recorded reasons (cap reached)"
REASON_SUPPRESSED = (
    "attribution text matching a secret shape was suppressed, never rendered "
    "(ADR-0009 rules: {rules})"
)
REASON_MODEL_CALL_SUPPRESSED = (
    "model-call text matching a secret shape was suppressed, never rendered "
    "(ADR-0009 rules: {rules})"
)
REASON_UNKNOWN_CALL = (
    "attribution names model_call_id {call_id!r}, which is not among the recorded model calls"
)
REASON_NO_SEAL = "capsule is not NovaSeal-sealed (.seal/manifest.dsse absent)"


# ---------------------------------------------------------------------------
# Input facts (produced by the collector; never guessed)
# ---------------------------------------------------------------------------


class ModelCallFacts(BaseModel):
    """One recorded ``model-calls.jsonl`` record, reduced to references and digests."""

    model_config = ConfigDict(frozen=True)

    line: int  # 1-based line number in model-calls.jsonl
    model_call_id: str | None = None
    system: str | None = None  # gen_ai.system
    request_model: str | None = None
    response_model: str | None = None
    status: str | None = None
    started_at: str | None = None
    input_digest: str | None = None  # sha256 of canonical JSON of gen_ai.request.messages
    output_digest: str | None = None  # sha256 of canonical JSON of gen_ai.response.choices
    record_sha256: str  # sha256 of the exact recorded JSONL line bytes
    suppressed_fields: list[str] = []  # secret-shaped string fields withheld (ADR-0009)


class InputFileFacts(BaseModel):
    """One file under the capsule's ``inputs/`` directory."""

    model_config = ConfigDict(frozen=True)

    path: str  # capsule-relative POSIX path
    sha256: str
    size_bytes: int
    bound: bool  # True when listed in (and matching) the capsule's evidence_digests


class ReasonFacts(BaseModel):
    """One recorded principal reason, verbatim except for secret-shaped suppression."""

    model_config = ConfigDict(frozen=True, strict=True)

    position: int  # 1-based order in the recorded list (never re-sorted)
    rank: int | None = None  # the rank as recorded (an integer), if the producer recorded one
    feature: str | None = None
    reason_code: str | None = None
    description: str | None = None
    contribution: Number | None = None  # as recorded; never recomputed or normalised
    suppressed_fields: list[str] = []


class AttributionFacts(BaseModel):
    """The recorded ``facets.feature_attribution`` block."""

    model_config = ConfigDict(frozen=True)

    facet_ref: str
    method: str | None = None
    producer: str | None = None
    model_call_id: str | None = None
    reasons: list[ReasonFacts] = []
    total_reasons: int = 0
    suppressed_fields: list[str] = []  # block-level fields (method/producer/model_call_id)
    suppressed_rule_ids: list[str] = []


class CapsuleFacts(BaseModel):
    """Everything the collector read from one capsule."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    capsule_ref: str
    run_status: str | None = None
    model_calls: list[ModelCallFacts] = []
    total_model_calls: int = 0
    model_calls_bound: bool = False
    model_call_suppressed_rule_ids: list[str] = []
    inputs: list[InputFileFacts] = []
    total_inputs: int = 0
    attribution: AttributionFacts | None = None
    seal_ref: str | None = None


# ---------------------------------------------------------------------------
# Output artifact
# ---------------------------------------------------------------------------


class PackRow(BaseModel):
    """One completeness row of the pack."""

    key: str
    status: RowStatus
    source_refs: list[str] = []
    reasons: list[str] = []  # machine-readable reasons when not complete
    evidence_source: EvidenceSource


class AdverseActionPack(BaseModel):
    """NF-278 artifact. Intentionally NO notice text, verdict, or credit-outcome field."""

    regime: str
    banner: str
    cfpb_honesty: str
    run_id: str
    capsule_ref: str
    run_status: str | None
    rows: list[PackRow]
    model_calls: list[ModelCallFacts]
    sealed_inputs: list[InputFileFacts]
    attribution_method: str | None
    attribution_producer: str | None
    attribution_suppressed_fields: list[str] = []
    principal_reasons: list[ReasonFacts]  # recorded order, recorded rank — never re-ranked
    seal_hint: str
    summary: dict[str, int]


def _row(key: str, status: RowStatus, refs: list[str], reasons: list[str]) -> PackRow:
    return PackRow(
        key=key,
        status=status,
        source_refs=refs if status != "missing" else [],
        reasons=reasons,
        evidence_source=source_for_status(status, gap_states=_GAP_STATES),
    )


def _model_call_row(facts: CapsuleFacts) -> PackRow:
    if not facts.model_calls:
        return _row("model_call", "missing", [], [REASON_NO_MODEL_CALL])
    reasons: list[str] = []
    if facts.total_model_calls > len(facts.model_calls):
        reasons.append(
            REASON_MODEL_CALLS_TRUNCATED.format(
                shown=len(facts.model_calls), total=facts.total_model_calls
            )
        )
    if not facts.model_calls_bound:
        reasons.append(REASON_MODEL_CALLS_UNBOUND)
    if facts.model_call_suppressed_rule_ids:
        rules = ", ".join(facts.model_call_suppressed_rule_ids)
        reasons.append(REASON_MODEL_CALL_SUPPRESSED.format(rules=rules))
    refs = [f"{facts.capsule_ref}/model-calls.jsonl#L{c.line}" for c in facts.model_calls]
    return _row("model_call", "partial" if reasons else "complete", refs, reasons)


def _inputs_row(facts: CapsuleFacts) -> PackRow:
    if not facts.inputs:
        return _row("sealed_inputs", "missing", [], [REASON_NO_INPUTS])
    reasons: list[str] = []
    unbound = [f for f in facts.inputs if not f.bound]
    if unbound:
        reasons.append(REASON_INPUTS_UNBOUND.format(n=len(unbound), m=len(facts.inputs)))
    if facts.total_inputs > len(facts.inputs):
        reasons.append(
            REASON_INPUTS_TRUNCATED.format(shown=len(facts.inputs), total=facts.total_inputs)
        )
    refs = [f"{facts.capsule_ref}/{f.path}" for f in facts.inputs]
    return _row("sealed_inputs", "partial" if reasons else "complete", refs, reasons)


def _reasons_row(facts: CapsuleFacts) -> PackRow:
    attr = facts.attribution
    if attr is None:
        return _row("principal_reasons", "missing", [], [REASON_NO_ATTRIBUTION])
    if not attr.reasons:
        return _row("principal_reasons", "missing", [], [REASON_NO_REASONS])
    reasons: list[str] = []
    if attr.total_reasons > len(attr.reasons):
        reasons.append(
            REASON_REASONS_TRUNCATED.format(shown=len(attr.reasons), total=attr.total_reasons)
        )
    if attr.suppressed_rule_ids:
        reasons.append(REASON_SUPPRESSED.format(rules=", ".join(attr.suppressed_rule_ids)))
    known = {c.model_call_id for c in facts.model_calls if c.model_call_id}
    if attr.model_call_id is not None and attr.model_call_id not in known:
        reasons.append(REASON_UNKNOWN_CALL.format(call_id=attr.model_call_id))
    status: RowStatus = "partial" if reasons else "complete"
    return _row("principal_reasons", status, [attr.facet_ref], reasons)


def _seal_row(facts: CapsuleFacts) -> PackRow:
    if facts.seal_ref is None:
        return _row("seal", "missing", [], [REASON_NO_SEAL])
    return _row("seal", "complete", [facts.seal_ref], [])


def build_adverse_action_pack(facts: CapsuleFacts) -> AdverseActionPack:
    """Render the NF-278 specific-reasons evidence pack from collected capsule facts (pure).

    Principal reasons are copied in the order and with the rank the producer recorded; this
    function never sorts, scores, or derives a reason. Every row is ``complete`` / ``partial`` /
    ``missing``; ``missing`` rows carry no source refs.
    """
    rows = [_model_call_row(facts), _inputs_row(facts), _reasons_row(facts), _seal_row(facts)]
    summary = {"complete": 0, "partial": 0, "missing": 0}
    for r in rows:
        summary[r.status] += 1
    attr = facts.attribution
    return AdverseActionPack(
        regime=ADVERSE_ACTION_REGIME,
        banner=FINANCE_HONESTY_BANNER,
        cfpb_honesty=CFPB_HONESTY_LINE,
        run_id=facts.run_id,
        capsule_ref=facts.capsule_ref,
        run_status=facts.run_status,
        rows=rows,
        model_calls=list(facts.model_calls),
        sealed_inputs=list(facts.inputs),
        attribution_method=attr.method if attr else None,
        attribution_producer=attr.producer if attr else None,
        attribution_suppressed_fields=list(attr.suppressed_fields) if attr else [],
        principal_reasons=list(attr.reasons) if attr else [],
        seal_hint=SEAL_VERIFY_HINT,
        summary=summary,
    )
