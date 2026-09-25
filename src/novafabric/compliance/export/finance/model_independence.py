"""Model-validation-independence evidence (ADR-0159 D2 / NF-276).

A pure **renderer** over the ADR-0058 maker-checker record. For each recorded promotion it states
whether the *checker* (validator) identity differs from the *maker* (developer) identity, and
rolls those per-record facts into one ``independence`` field marked ``complete`` / ``partial`` /
``missing`` with source refs or a machine-readable reason.

It **asserts only that independence was recorded** — never that the validation was *sufficient*,
never a model-risk rating, never a compliance determination (there is intentionally no verdict or
score field). It never re-implements the approval gate: the gate itself lives in
:mod:`novafabric.registry.service` (``propose_promotion`` / ``approve_promotion``) and
:func:`novafabric.promote.verifier.verify_sod`; the records this module renders are *read* from
those by :mod:`.model_independence_collect`. Nothing is fabricated — a promotion with no recorded
checker, a single-identity approval, a bypass, or a record that failed SoD verification is
reported as ``missing`` with the reason.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..provenance import EvidenceSource, source_for_status

#: Statuses denoting a checked gap (evidence expected, absent) → ``unverifiable`` (ADR-0197 I-2).
_INDEPENDENCE_GAP_STATES = frozenset({"missing"})

#: Version-pinned regime tag: the SR 26-2 independent-validation pillar, evidenced by ADR-0058.
INDEPENDENCE_REGIME = "SR 26-2 (2026-04-17) independent validation — ADR-0058 maker-checker record"

#: Binding compliance-honesty banner (ADR-0159 §15 — carried in every artifact and CLI output).
FINANCE_HONESTY_BANNER = (
    "This artifact SUPPORTS a firm's own regulated determination; it does not guarantee "
    "compliance, performs no model validation or other regulated assessment, assigns no "
    "rating, makes no record immutable, and transmits nothing to any regulator. NovaFabric "
    "renders recorded facts; it never fabricates a field. Whether it satisfies a specific "
    "obligation is for qualified financial regulatory counsel to determine."
)

#: Machine-readable reasons (stable strings — downstream tooling may match on them).
REASON_NO_RECORD = "no ADR-0058 maker-checker record found for {model}"
REASON_SINGLE_IDENTITY = "single-identity approval"
REASON_NO_CHECKER = "no checker (validator) counter-signature recorded (state={state})"
REASON_BYPASS = "SoD bypass used — no independent checker recorded"
REASON_UNVERIFIED = "maker-checker record failed SoD verification: {detail}"
REASON_PARTIAL = (
    "independence recorded for {n} of {m} maker-checker records; see per-record reasons"
)
REASON_NONE_INDEPENDENT = "no independently counter-signed maker-checker record"

RecordSource = Literal["registry_promotion", "seal_promote_bundle"]
IndependenceStatus = Literal["complete", "partial", "missing"]


class MakerCheckerRecord(BaseModel):
    """One ADR-0058 maker-checker record as read from its store (never constructed from guesses).

    ``maker`` is the developer / proposer identity; ``checker`` the validator / approver identity.
    Key fingerprints are compared too when both are recorded (local-mode Ed25519 identities).
    ``verification_failure`` carries the SoD verifier's message when the record exists but did not
    verify (signature, policy, digest, or ordering failure) — independence is then not evidenced.
    """

    model_config = ConfigDict(frozen=True)

    source: RecordSource
    record_ref: str  # e.g. "registry://promotion_proposals/<id>" or "seal://promote/<capsule>/<id>"
    subject: str  # asset "name@version" or capsule id
    state: str  # "open" | "approved" | "rejected" | "verified" | "unverified" …
    maker: str | None = None
    checker: str | None = None
    maker_key_fp: str | None = None
    checker_key_fp: str | None = None
    proposed_at: str | None = None
    approved_at: str | None = None
    bypass_used: bool = False
    verification_failure: str | None = None


class IndependenceRecord(BaseModel):
    """A maker-checker record plus the rendered independence status for it."""

    record: MakerCheckerRecord
    status: IndependenceStatus
    reason: str | None = None  # machine-readable reason when not complete
    evidence_source: EvidenceSource


class IndependenceField(BaseModel):
    """The roll-up ``independence`` field across every record for the model."""

    status: IndependenceStatus
    source_refs: list[str] = []  # refs of the records that evidence it (empty when missing)
    reason: str | None = None
    evidence_source: EvidenceSource


class ModelIndependenceFile(BaseModel):
    """NF-276 validation-independence artifact. Intentionally NO verdict/rating field."""

    regime: str
    banner: str
    model_id: str
    independence: IndependenceField
    records: list[IndependenceRecord]
    summary: dict[str, int]  # counts of complete / missing across records


def _same_identity(record: MakerCheckerRecord) -> bool:
    """True when the recorded maker and checker are the same identity or the same key."""
    if record.maker is not None and record.maker == record.checker:
        return True
    return (
        record.maker_key_fp is not None
        and record.checker_key_fp is not None
        and record.maker_key_fp == record.checker_key_fp
    )


def classify_record(record: MakerCheckerRecord) -> IndependenceRecord:
    """Render the independence status of one recorded promotion (pure; no IO).

    Order matters: a bypass or a failed verification means the recorded identities cannot be
    relied on, so those are reported before the identity comparison.
    """
    reason: str | None
    if record.bypass_used:
        reason = REASON_BYPASS
    elif record.verification_failure is not None:
        reason = REASON_UNVERIFIED.format(detail=record.verification_failure)
    elif record.checker is None:
        reason = REASON_NO_CHECKER.format(state=record.state)
    elif _same_identity(record):
        reason = REASON_SINGLE_IDENTITY
    else:
        reason = None
    status: IndependenceStatus = "complete" if reason is None else "missing"
    return IndependenceRecord(
        record=record,
        status=status,
        reason=reason,
        evidence_source=source_for_status(status, gap_states=_INDEPENDENCE_GAP_STATES),
    )


def _roll_up(model_id: str, rendered: list[IndependenceRecord]) -> IndependenceField:
    """Combine per-record statuses into the single ``independence`` field."""
    complete = [r for r in rendered if r.status == "complete"]
    status: IndependenceStatus
    refs: list[str] = [r.record.record_ref for r in complete]
    if not rendered:
        status, reason = "missing", REASON_NO_RECORD.format(model=model_id)
    elif len(complete) == len(rendered):
        status, reason = "complete", None
    elif complete:
        status = "partial"
        reason = REASON_PARTIAL.format(n=len(complete), m=len(rendered))
    else:
        status = "missing"
        single = [r for r in rendered if r.reason == REASON_SINGLE_IDENTITY]
        reason = REASON_SINGLE_IDENTITY if single else REASON_NONE_INDEPENDENT
    return IndependenceField(
        status=status,
        source_refs=refs,
        reason=reason,
        evidence_source=source_for_status(status, gap_states=_INDEPENDENCE_GAP_STATES),
    )


def build_model_independence_file(
    *, model_id: str, records: Iterable[MakerCheckerRecord]
) -> ModelIndependenceFile:
    """Render the NF-276 validation-independence artifact for ``model_id`` (pure; no IO).

    Every record is classified by :func:`classify_record`; the roll-up is ``complete`` when every
    record evidences a distinct checker, ``partial`` when only some do, and ``missing`` otherwise
    (``"single-identity approval"`` when that is why). No record is invented.
    """
    rendered = [classify_record(r) for r in records]
    summary = {"complete": 0, "missing": 0}
    for r in rendered:
        summary[r.status] += 1
    return ModelIndependenceFile(
        regime=INDEPENDENCE_REGIME,
        banner=FINANCE_HONESTY_BANNER,
        model_id=model_id,
        independence=_roll_up(model_id, rendered),
        records=rendered,
        summary=summary,
    )
