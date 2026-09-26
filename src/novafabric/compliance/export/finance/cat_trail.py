"""CAT-style lifecycle agent-event trail (ADR-0159 D6 second half / NF-280).

A pure **renderer** over facts read from one sealed Run Capsule by :mod:`.cat_collect`. It orders
the events the capsule actually recorded into a self-contained, firm-owned agent-event trail
*modelled on* the SEC Rule 613 Consolidated Audit Trail event lifecycle — lifecycle stage, actor
identity ref, and timestamp per event — and marks each lifecycle stage ``complete`` / ``partial``
/ ``missing`` with source refs or a machine-readable reason.

**Stage mapping (explicit; nothing else is read and no event is ever synthesised):**

=================  ================================================  ==============================
Lifecycle stage    Capsule source                                    Actor identity ref (recorded)
=================  ================================================  ==============================
``origination``    ``capsule.yaml`` ``created_at`` (run started)     none recorded at run level
``decision``       ``model-calls.jsonl`` (one event per record)      response/request model ref
``authorization``  ``tool-permission-events.jsonl`` (cap-004) and    ``authorising_identity`` /
                   ``human_approvals.jsonl`` (maker-checker)          ``approver_id``
``action``         ``tool-calls.jsonl`` (one event per record)       ``tool_name``
``disposition``    ``capsule.yaml`` ``finished_at`` + ``status``     none recorded at run level
=================  ================================================  ==============================

Events are ordered by their recorded timestamp (UTC-normalised), ties broken by lifecycle-stage
order, then source stream, then line — deterministic. An event without a recorded timestamp is
kept (never dropped, never given a guessed time) and placed after the timestamped events in
source order; its stage row is ``partial``.

**Completeness roll-up** (spec §4.1: *missing lifecycle stage → partial*): the trail is
``complete`` only when every *required* stage (:data:`REQUIRED_STAGES`) has at least one event
and no stage row is ``partial``; ``missing`` when the capsule recorded no event at all; otherwise
``partial``. ``authorization`` is reported as its own row but is **not** required — a run whose
policy required no permission decision or approval legitimately records none.

It **renders evidence, never a submission**: there is no CAT reporter id, no CAT event-type code,
no CAT Industry Member Reporting Technical Specifications field, and no transmit/submitted field.
Nothing here opens a network connection; the artifact is a firm-owned file only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..provenance import EvidenceSource, source_for_status
from .model_independence import FINANCE_HONESTY_BANNER

__all__ = [
    "CAT_HONESTY_LINE",
    "CAT_REGIME",
    "FINANCE_HONESTY_BANNER",
    "LIFECYCLE_STAGES",
    "REQUIRED_STAGES",
    "SEAL_VERIFY_HINT",
    "CatTrail",
    "EventFacts",
    "StageRow",
    "StreamFacts",
    "TrailFacts",
    "build_cat_trail",
]

_GAP_STATES = frozenset({"missing"})

RowStatus = Literal["complete", "partial", "missing"]
Stage = Literal["origination", "decision", "authorization", "action", "disposition"]

#: Lifecycle stages in lifecycle order (also the ordering tie-break).
LIFECYCLE_STAGES: tuple[Stage, ...] = (
    "origination",
    "decision",
    "authorization",
    "action",
    "disposition",
)

#: Stages whose absence makes the trail ``partial`` (spec §4.1). ``authorization`` is optional.
REQUIRED_STAGES: tuple[Stage, ...] = ("origination", "decision", "action", "disposition")

#: Version-pinned regime text (ADR-0159 D1 / spec item 14). The pattern source, not a claim of
#: conformance: no CAT technical-specification field set is implemented.
CAT_REGIME = (
    "Modelled on the SEC Rule 613 (17 CFR 242.613; adopted 2012-07-11, Release No. 34-67457) "
    "Consolidated Audit Trail event lifecycle and the CAT NMS Plan (approved 2016-11-15, "
    "Release No. 34-79318); context: SEC CAT concept release (2026-04-20, FR Doc. 2026-07651)"
)

#: Mandatory CAT honesty line (ADR-0159 D6 / spec item 13) — carried in every artifact and output.
CAT_HONESTY_LINE = (
    "This is a firm-owned, self-hosted evidence trail of what the sealed capsule recorded, "
    "modelled on the CAT event lifecycle. It is NOT a CAT submission, is not in the CAT "
    "Industry Member Reporting Technical Specifications format, carries no CAT reporter or "
    "event-type code, and was not transmitted anywhere: NovaFabric never connects to the CAT "
    "central repository or a CAT plan processor and runs no reporting service. Whether and how "
    "any of it is reported is the firm's determination."
)

SEAL_VERIFY_HINT = (
    "seal presence only; re-verify the DSSE signature and evidence digests with `nova verify`"
)

#: Machine-readable reasons (stable strings — downstream tooling may match on them).
REASON_NO_EVENT = "no {stage} event recorded ({sources})"
REASON_TRUNCATED = "rendered {shown} of {total} recorded {stream} events (cap reached)"
REASON_UNBOUND = "{stream} is not bound by the capsule's evidence_digests (pre-ADR-0251 capsule)"
REASON_SUPPRESSED = (
    "{stream} text matching a secret shape was suppressed, never rendered (ADR-0009 rules: {rules})"
)
REASON_NO_TIMESTAMP = (
    "{n} {stage} event(s) carry no recorded timestamp; ordered by source position after the "
    "timestamped events"
)
REASON_TRAIL_STAGE_MISSING = "required lifecycle stage(s) not recorded: {stages}"
REASON_TRAIL_STAGE_PARTIAL = "lifecycle stage row(s) partial: {stages}"
REASON_TRAIL_EMPTY = "the capsule recorded no lifecycle event"

#: Where each stage's events come from (rendered in the ``missing`` reason).
STAGE_SOURCES: dict[Stage, str] = {
    "origination": "capsule.yaml created_at",
    "decision": "model-calls.jsonl",
    "authorization": "tool-permission-events.jsonl, human_approvals.jsonl",
    "action": "tool-calls.jsonl",
    "disposition": "capsule.yaml finished_at/status",
}


# ---------------------------------------------------------------------------
# Input facts (produced by the collector; never guessed)
# ---------------------------------------------------------------------------


class EventFacts(BaseModel):
    """One recorded lifecycle event, reduced to references, identity refs, and timestamps."""

    model_config = ConfigDict(frozen=True)

    stage: Stage
    event_type: str  # run_started | model_call | tool_permission_decision | human_approval | …
    source_ref: str  # capsule-relative ref, e.g. ``model-calls.jsonl#L3``
    stream_index: int  # position of the source stream in collection order (ordering tie-break)
    line: int  # 1-based line in the stream (0 for capsule.yaml fields)
    record_sha256: str | None = None  # sha256 of the exact recorded JSONL line bytes
    event_id: str | None = None
    timestamp: str | None = None  # recorded start/decision time, verbatim
    timestamp_utc: str | None = None  # the same instant normalised to UTC (ordering key)
    ended_at: str | None = None  # recorded end time, verbatim, when the record carries one
    actor_kind: str | None = None  # model | tool | policy-principal | human-approver
    actor_ref: str | None = None  # the recorded identity ref — never inferred
    actor_detail: str | None = None  # gen_ai.system / tool_provider / policy_id as recorded
    approver_ref: str | None = None  # recorded human approver for a permission decision
    outcome: str | None = None  # recorded status / decision / action
    caused_by_ref: str | None = None  # recorded upstream id (e.g. a tool call's agent_call_id)
    subject_ref: str | None = None  # recorded subject (tool name / target run id)
    suppressed_fields: list[str] = []  # secret-shaped fields withheld (ADR-0009)


class StreamFacts(BaseModel):
    """Collection bookkeeping for one source stream."""

    model_config = ConfigDict(frozen=True)

    name: str  # e.g. ``model-calls.jsonl``
    stage: Stage
    present: bool
    bound: bool  # listed in (and matching) the capsule's evidence_digests
    total: int  # records in the stream
    rendered: int  # records rendered (≤ the per-stream cap)
    suppressed_rule_ids: list[str] = []


class TrailFacts(BaseModel):
    """Everything the collector read from one capsule."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    capsule_ref: str
    run_status: str | None = None
    events: list[EventFacts] = []
    streams: list[StreamFacts] = []
    manifest_suppressed_rule_ids: list[str] = []
    seal_ref: str | None = None


# ---------------------------------------------------------------------------
# Output artifact
# ---------------------------------------------------------------------------


class StageRow(BaseModel):
    """One lifecycle-stage completeness row."""

    key: Stage
    status: RowStatus
    required: bool
    event_count: int
    source_refs: list[str] = []
    reasons: list[str] = []
    evidence_source: EvidenceSource


class TrailEvent(EventFacts):
    """An event as rendered: its 1-based position in the lifecycle-ordered trail."""

    sequence: int


class CatTrail(BaseModel):
    """NF-280 artifact. Intentionally NO submission, reporter-id, or transmitted field."""

    regime: str
    banner: str
    cat_honesty: str
    run_id: str
    capsule_ref: str
    run_status: str | None
    stage_mapping: dict[str, str]
    required_stages: list[str]
    trail_status: RowStatus
    trail_reasons: list[str]
    rows: list[StageRow]
    events: list[TrailEvent]
    streams: list[StreamFacts]
    seal_ref: str | None
    seal_hint: str
    summary: dict[str, int]


def _order_key(e: EventFacts) -> tuple[int, str, int, int, int]:
    stage_ix = LIFECYCLE_STAGES.index(e.stage)
    if e.timestamp_utc is None:
        return (1, "", e.stream_index, e.line, stage_ix)
    return (0, e.timestamp_utc, stage_ix, e.stream_index, e.line)


def _stage_row(stage: Stage, facts: TrailFacts, events: list[EventFacts]) -> StageRow:
    mine = [e for e in events if e.stage == stage]
    required = stage in REQUIRED_STAGES
    if not mine:
        return StageRow(
            key=stage,
            status="missing",
            required=required,
            event_count=0,
            reasons=[REASON_NO_EVENT.format(stage=stage, sources=STAGE_SOURCES[stage])],
            evidence_source=source_for_status("missing", gap_states=_GAP_STATES),
        )
    reasons: list[str] = []
    for s in facts.streams:
        if s.stage != stage or not s.present:
            continue
        if s.total > s.rendered:
            reasons.append(REASON_TRUNCATED.format(shown=s.rendered, total=s.total, stream=s.name))
        if not s.bound:
            reasons.append(REASON_UNBOUND.format(stream=s.name))
        if s.suppressed_rule_ids:
            reasons.append(
                REASON_SUPPRESSED.format(stream=s.name, rules=", ".join(s.suppressed_rule_ids))
            )
    if stage in ("origination", "disposition") and facts.manifest_suppressed_rule_ids:
        rules = ", ".join(facts.manifest_suppressed_rule_ids)
        reasons.append(REASON_SUPPRESSED.format(stream="capsule.yaml", rules=rules))
    untimed = sum(1 for e in mine if e.timestamp_utc is None)
    if untimed:
        reasons.append(REASON_NO_TIMESTAMP.format(n=untimed, stage=stage))
    status: RowStatus = "partial" if reasons else "complete"
    return StageRow(
        key=stage,
        status=status,
        required=required,
        event_count=len(mine),
        source_refs=[f"{facts.capsule_ref}/{e.source_ref}" for e in mine],
        reasons=reasons,
        evidence_source=source_for_status(status, gap_states=_GAP_STATES),
    )


def _roll_up(rows: list[StageRow], n_events: int) -> tuple[RowStatus, list[str]]:
    if n_events == 0:
        return "missing", [REASON_TRAIL_EMPTY]
    reasons: list[str] = []
    absent = [r.key for r in rows if r.required and r.status == "missing"]
    if absent:
        reasons.append(REASON_TRAIL_STAGE_MISSING.format(stages=", ".join(absent)))
    partial = [r.key for r in rows if r.status == "partial"]
    if partial:
        reasons.append(REASON_TRAIL_STAGE_PARTIAL.format(stages=", ".join(partial)))
    return ("partial" if reasons else "complete"), reasons


def build_cat_trail(facts: TrailFacts) -> CatTrail:
    """Render the NF-280 lifecycle-ordered agent-event trail from collected facts (pure).

    Events are copied as recorded and only *ordered*; this function never creates, merges,
    or drops an event. ``missing`` stage rows carry no source refs.
    """
    ordered = sorted(facts.events, key=_order_key)
    rows = [_stage_row(stage, facts, ordered) for stage in LIFECYCLE_STAGES]
    trail_status, trail_reasons = _roll_up(rows, len(ordered))
    summary = {"complete": 0, "partial": 0, "missing": 0}
    for r in rows:
        summary[r.status] += 1
    return CatTrail(
        regime=CAT_REGIME,
        banner=FINANCE_HONESTY_BANNER,
        cat_honesty=CAT_HONESTY_LINE,
        run_id=facts.run_id,
        capsule_ref=facts.capsule_ref,
        run_status=facts.run_status,
        stage_mapping={str(k): v for k, v in STAGE_SOURCES.items()},
        required_stages=list(REQUIRED_STAGES),
        trail_status=trail_status,
        trail_reasons=trail_reasons,
        rows=rows,
        events=[TrailEvent(sequence=i, **e.model_dump()) for i, e in enumerate(ordered, start=1)],
        streams=list(facts.streams),
        seal_ref=facts.seal_ref,
        seal_hint=SEAL_VERIFY_HINT,
        summary=summary,
    )
