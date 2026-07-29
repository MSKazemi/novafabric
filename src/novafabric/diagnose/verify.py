"""Hypothesis verification via counterfactual intervention replay (ADR-0101).

First slice of intervention-verified attribution (NF-017 subset + the NF-022
verdict field, experimental): take the top diagnose-produced failure hypothesis
(ADR-0084), deterministically synthesize an ``InterventionSpec`` (ADR-0086) that
tests it, replay the capsule counterfactually under mocked semantics, and record
whether the intervention flips the outcome (failure -> success).

The verdict is **evidence-based, never guessed**:

* ``CONFIRMED``    — the intervention replay re-executed the capsule's command
  and the original failure flipped to success (exit code 0).
* ``REFUTED``      — the re-execution still failed after the intervention.
* ``INCONCLUSIVE`` — the flip is not measurable: no hypothesis, the original run
  did not fail, the hypothesis class is not auto-mappable, the capsule has no
  re-executable command, or the replay aborted/errored. The reason is always
  recorded on the verification record.

Deterministic auto-mappable subset (this slice): **model-call hypotheses only**.
The corrective edit clears the error signal at the implicated model call via a
``mutate_payload`` substitution; the mocked re-execution then observes the
corrected model-call queue. Tool/span/run hypotheses are honestly reported as
not auto-intervenable — a tool-stream substitution never reaches the mocked
re-execution queue, so any verdict for them would be fabricated evidence.

Fidelity bound (ADR-0086, stated honestly): the counterfactual runs zero-token
under mocked semantics — the verdict tests control-flow and downstream handling
of the recorded run, not fresh model behavior.

This module also implements the **counterfactual root-cause search** (ADR-0101
§NF-018): given a failed capsule, sweep the NF-019 causal-root candidates — in
their existing shallowest/earliest-first rank order, which is exactly the
pruning the ADR calls for over a naive linear sweep of every step — running a
bounded number of zero-token mocked intervention replays until one flips the
outcome. The first confirmed flip is the decisive root cause, recorded with its
counterfactual evidence; every attempt (confirmed, refuted, or honestly
unmappable) is kept on the search record so the search is itself auditable.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from novafabric.diagnose.attribution import (
    AgentErrorTaxonomy,
    RunAttribution,
    StepAttribution,
    _read_jsonl,
)
from novafabric.diagnose.causal_graph import causal_root_candidates

_FAILURE_STATUSES = {"failure", "failed", "error"}

_FIDELITY_NOTE = (
    "Counterfactual replay runs zero-token under mocked semantics (ADR-0086); "
    "the verdict tests control-flow and downstream handling, not fresh model "
    "behavior (ADR-0101)."
)

#: NF-018 search bounds — a fixed default plus a hard ceiling so a caller-supplied
#: value can never make the search sweep an unbounded number of replays.
_DEFAULT_MAX_INTERVENTIONS = 8
_MAX_INTERVENTIONS_CEILING = 50

# The deterministic corrective edit: clear every error signal the attribution
# pass reads (attribution._has_error) at the implicated step.
_CORRECTIVE_PAYLOAD: dict[str, Any] = {
    "status": "ok",
    "error": None,
    "exception": None,
    "traceback_ref": None,
    "counterfactual_note": (
        "ADR-0101 auto-intervention: error signal cleared at the implicated step"
    ),
}


class Verdict(str, Enum):
    """NF-022 evidence-based verification verdicts."""

    CONFIRMED = "CONFIRMED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class UnmappableHypothesisError(Exception):
    """Raised when no InterventionSpec can be deterministically synthesized."""


@dataclass
class HypothesisVerification:
    """One verified (or honestly unverifiable) failure hypothesis."""

    verdict: Verdict
    reason: str
    hypothesis: dict[str, Any] | None
    intervention: dict[str, Any] | None
    original_outcome: dict[str, Any]
    counterfactual_outcome: dict[str, Any] | None
    intervened_capsule: str | None
    # NF-020: the AgentErrorTaxonomy tag carried onto the sealed verification
    # record itself (not just nested inside `hypothesis`), so a consumer can
    # filter/aggregate verified attributions by taxonomy without unpacking it.
    taxonomy: AgentErrorTaxonomy | None = None
    note: str = _FIDELITY_NOTE

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "hypothesis": self.hypothesis,
            "intervention": self.intervention,
            "original_outcome": self.original_outcome,
            "counterfactual_outcome": self.counterfactual_outcome,
            "intervened_capsule": self.intervened_capsule,
            "taxonomy": self.taxonomy.value if self.taxonomy is not None else None,
            "note": self.note,
        }


def synthesize_intervention_payload(
    step: StepAttribution, model_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    """Deterministically map a hypothesis to an InterventionSpec payload.

    Only model-call hypotheses are auto-mappable in this slice; anything else
    raises :class:`UnmappableHypothesisError` with an honest reason.
    """
    if step.kind != "model":
        raise UnmappableHypothesisError(
            f"cannot auto-intervene for this hypothesis class: '{step.kind}' — "
            "only model-call hypotheses are auto-mappable in this slice "
            "(ADR-0101 first slice)"
        )
    for index, record in enumerate(model_calls):
        rid = record.get("span_id") or record.get("id") or record.get("call_id")
        if rid is not None and str(rid) == step.step_id:
            return {
                "target": {"stream": "model-calls", "event_index": index},
                "mutate_payload": dict(_CORRECTIVE_PAYLOAD),
            }
    raise UnmappableHypothesisError(
        f"cannot auto-intervene: implicated step '{step.step_id}' was not found "
        "in the model-calls stream"
    )


def _verify_step(
    capsule_dir: Path,
    step: StepAttribution,
    model_calls: list[dict[str, Any]],
    original_outcome: dict[str, Any],
    base_dir: Path,
) -> HypothesisVerification:
    """Drive one intervention replay for *step* and record the NF-022 verdict.

    Shared by :func:`verify_hypothesis` (tests the single top hypothesis) and
    :func:`search_root_cause` (NF-018, sweeps several ranked candidates) so the
    replay-driving logic — synthesize, replay, classify the outcome — is written
    once. Never raises for an unverifiable/unflippable step; that is always an
    ``INCONCLUSIVE`` or ``REFUTED`` verdict with the reason recorded.
    """
    from novafabric.replay._engine import ReplayEngine
    from novafabric.replay._flags import ReplayFlags
    from novafabric.replay._intervention import InterventionError, InterventionSpec

    hypothesis = step.as_dict()
    taxonomy = step.taxonomy

    def _inconclusive(
        reason: str,
        *,
        intervention: dict[str, Any] | None = None,
        counterfactual: dict[str, Any] | None = None,
        intervened: str | None = None,
    ) -> HypothesisVerification:
        return HypothesisVerification(
            verdict=Verdict.INCONCLUSIVE,
            reason=reason,
            hypothesis=hypothesis,
            intervention=intervention,
            original_outcome=original_outcome,
            counterfactual_outcome=counterfactual,
            intervened_capsule=intervened,
            taxonomy=taxonomy,
        )

    try:
        payload = synthesize_intervention_payload(step, model_calls)
    except UnmappableHypothesisError as exc:
        return _inconclusive(str(exc))

    spec = InterventionSpec.model_validate(payload)
    intervention_meta: dict[str, Any] = {
        **spec.describe(),
        "payload": payload["mutate_payload"],
    }

    with tempfile.TemporaryDirectory(prefix="nf_diagnose_intervene_") as tmp:
        spec_path = Path(tmp) / "intervention.yaml"
        spec_path.write_text(yaml.dump(payload, allow_unicode=True))
        flags = ReplayFlags(mode="intervention", intervention_file=spec_path)
        engine = ReplayEngine(capsule_dir=capsule_dir, flags=flags, base_dir=base_dir)
        try:
            result = engine.run()
        except InterventionError as exc:
            return _inconclusive(
                f"intervention replay failed: {exc}", intervention=intervention_meta
            )

    counterfactual = {
        "replay_id": result.replay_id,
        "status": result.status,
        "exit_code": result.exit_code,
    }
    intervened = str(base_dir / result.replay_id)

    if result.status == "aborted":
        message = (result.error or {}).get("message", "replay aborted")
        return _inconclusive(
            f"intervention replay aborted: {message}",
            intervention=intervention_meta,
            counterfactual=counterfactual,
            intervened=intervened,
        )
    if result.exit_code is None:
        return _inconclusive(
            "capsule has no re-executable command; the outcome flip is not "
            "measurable under mocked semantics",
            intervention=intervention_meta,
            counterfactual=counterfactual,
            intervened=intervened,
        )
    if result.status == "success":
        return HypothesisVerification(
            verdict=Verdict.CONFIRMED,
            reason=(
                "intervention flipped the outcome: the original failure "
                "re-executed to success (exit code 0)"
            ),
            hypothesis=hypothesis,
            intervention=intervention_meta,
            original_outcome=original_outcome,
            counterfactual_outcome=counterfactual,
            intervened_capsule=intervened,
            taxonomy=taxonomy,
        )
    return HypothesisVerification(
        verdict=Verdict.REFUTED,
        reason=(
            "intervention did not flip the outcome: the re-execution still "
            f"failed (exit code {result.exit_code})"
        ),
        hypothesis=hypothesis,
        intervention=intervention_meta,
        original_outcome=original_outcome,
        counterfactual_outcome=counterfactual,
        intervened_capsule=intervened,
        taxonomy=taxonomy,
    )


def _load_model_calls(capsule_dir: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    model_calls_ref = str(manifest.get("model_calls_ref", "model-calls.jsonl"))
    return _read_jsonl(capsule_dir / model_calls_ref)


def verify_hypothesis(
    capsule_dir: Path,
    attribution: RunAttribution,
    replays_base_dir: Path | None = None,
) -> HypothesisVerification:
    """Verify the top hypothesis of *attribution* with an intervention replay.

    Drives the shipped ADR-0086 engine in-process (never reimplements it); the
    source capsule stays read-only and the intervened output capsule is
    hard-marked ``replay_mode: intervention``. Returns the NF-022 verification
    record; never raises for an unverifiable hypothesis — that is an
    ``INCONCLUSIVE`` verdict with the reason recorded.
    """
    capsule_dir = Path(capsule_dir)
    base_dir = replays_base_dir or (Path.cwd() / ".novafabric" / "replays")
    original_outcome = {"status": attribution.status}

    if attribution.responsible is None:
        return HypothesisVerification(
            verdict=Verdict.INCONCLUSIVE,
            reason="no failure hypothesis produced by attribution; nothing to intervene on",
            hypothesis=None,
            intervention=None,
            original_outcome=original_outcome,
            counterfactual_outcome=None,
            intervened_capsule=None,
        )

    if attribution.status.lower() not in _FAILURE_STATUSES:
        return HypothesisVerification(
            verdict=Verdict.INCONCLUSIVE,
            reason=(
                f"original run status is '{attribution.status}', not a failure; "
                "there is no failure outcome to flip"
            ),
            hypothesis=attribution.responsible.as_dict(),
            intervention=None,
            original_outcome=original_outcome,
            counterfactual_outcome=None,
            intervened_capsule=None,
            taxonomy=attribution.responsible.taxonomy,
        )

    manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text()) or {}
    model_calls = _load_model_calls(capsule_dir, manifest)
    return _verify_step(
        capsule_dir, attribution.responsible, model_calls, original_outcome, base_dir
    )


@dataclass
class RootCauseAttempt:
    """One candidate tested during an NF-018 counterfactual root-cause search."""

    step_id: str
    name: str
    kind: str
    taxonomy: AgentErrorTaxonomy
    verdict: Verdict
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "kind": self.kind,
            "taxonomy": self.taxonomy.value,
            "verdict": self.verdict.value,
            "reason": self.reason,
        }


@dataclass
class RootCauseSearch:
    """Result of an NF-018 counterfactual root-cause search over a failed run."""

    run_id: str
    status: str
    confirmed: HypothesisVerification | None
    attempts: list[RootCauseAttempt] = field(default_factory=list)
    candidates_considered: int = 0
    bounded: bool = False
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "confirmed": self.confirmed.as_dict() if self.confirmed else None,
            "attempts": [a.as_dict() for a in self.attempts],
            "candidates_considered": self.candidates_considered,
            "bounded": self.bounded,
            "note": self.note,
        }


def search_root_cause(
    capsule_dir: Path,
    replays_base_dir: Path | None = None,
    max_interventions: int = _DEFAULT_MAX_INTERVENTIONS,
) -> RootCauseSearch:
    """NF-018: find the earliest step whose correction flips failure -> success.

    Sweeps the NF-019 causal-root candidates (:func:`causal_root_candidates`) in
    their existing shallowest/earliest-first rank order — the pruning ADR-0101
    calls for over a naive linear sweep of every step — driving a bounded number
    of zero-token mocked intervention replays (:func:`_verify_step`, the same
    machinery :func:`verify_hypothesis` uses) until one flips the outcome. The
    search is capped at *max_interventions* (clamped to
    ``[1, _MAX_INTERVENTIONS_CEILING]``) replays so a pathological capsule with
    many causal roots can never make this run unboundedly long. Every attempt —
    confirmed, refuted, or honestly unmappable — is kept on the result so the
    search itself is auditable, not just its winner.

    Returns a :class:`RootCauseSearch` with ``confirmed=None`` when no candidate
    within the bound flips the outcome; ``bounded=True`` then records that more
    causal-root candidates existed than were searched.
    """
    capsule_dir = Path(capsule_dir)
    capped = max(1, min(int(max_interventions), _MAX_INTERVENTIONS_CEILING))

    causal = causal_root_candidates(capsule_dir)

    if causal.status.lower() not in _FAILURE_STATUSES:
        return RootCauseSearch(
            run_id=causal.run_id,
            status=causal.status,
            confirmed=None,
            note=(
                f"original run status is '{causal.status}', not a failure; "
                "there is no failure outcome to search for"
            ),
        )

    if not causal.root_candidates:
        return RootCauseSearch(
            run_id=causal.run_id,
            status=causal.status,
            confirmed=None,
            note="no failing causal-root candidate found; nothing to search",
        )

    manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text()) or {}
    model_calls = _load_model_calls(capsule_dir, manifest)
    base_dir = replays_base_dir or (Path.cwd() / ".novafabric" / "replays")
    original_outcome = {"status": causal.status}

    considered = causal.root_candidates[:capped]
    attempts: list[RootCauseAttempt] = []
    confirmed: HypothesisVerification | None = None

    for candidate in considered:
        step = StepAttribution(
            step_id=candidate.step_id,
            name=candidate.name,
            kind=candidate.kind,
            score=candidate.rank_score,
            taxonomy=candidate.taxonomy,
            rationale=candidate.rationale,
        )
        verification = _verify_step(
            capsule_dir, step, model_calls, original_outcome, base_dir
        )
        attempts.append(
            RootCauseAttempt(
                step_id=candidate.step_id,
                name=candidate.name,
                kind=candidate.kind,
                taxonomy=candidate.taxonomy,
                verdict=verification.verdict,
                reason=verification.reason,
            )
        )
        if verification.verdict is Verdict.CONFIRMED:
            confirmed = verification
            break

    bounded = confirmed is None and len(causal.root_candidates) > capped
    if confirmed is not None:
        note = (
            f"decisive root cause found after {len(attempts)} of "
            f"{len(causal.root_candidates)} causal-root candidate(s) "
            "(NF-018 search, confirmed by replay — NF-022)."
        )
    elif bounded:
        note = (
            f"no candidate flipped the outcome within the bound of {capped} "
            f"intervention(s); {len(causal.root_candidates)} causal-root "
            "candidate(s) existed in total — search was bounded, not exhaustive."
        )
    else:
        note = (
            f"no candidate flipped the outcome across all "
            f"{len(causal.root_candidates)} causal-root candidate(s) — "
            "the search was exhaustive over this ranking."
        )

    return RootCauseSearch(
        run_id=causal.run_id,
        status=causal.status,
        confirmed=confirmed,
        attempts=attempts,
        candidates_considered=len(attempts),
        bounded=bounded,
        note=note,
    )
