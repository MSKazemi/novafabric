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
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from novafabric.diagnose.attribution import (
    RunAttribution,
    StepAttribution,
    _read_jsonl,
)

_FAILURE_STATUSES = {"failure", "failed", "error"}

_FIDELITY_NOTE = (
    "Counterfactual replay runs zero-token under mocked semantics (ADR-0086); "
    "the verdict tests control-flow and downstream handling, not fresh model "
    "behavior (ADR-0101)."
)

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
    from novafabric.replay._engine import ReplayEngine
    from novafabric.replay._flags import ReplayFlags
    from novafabric.replay._intervention import InterventionError, InterventionSpec

    capsule_dir = Path(capsule_dir)
    base_dir = replays_base_dir or (Path.cwd() / ".novafabric" / "replays")
    original_outcome = {"status": attribution.status}

    def _inconclusive(
        reason: str,
        *,
        hypothesis: dict[str, Any] | None = None,
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
        )

    if attribution.responsible is None:
        return _inconclusive(
            "no failure hypothesis produced by attribution; nothing to intervene on"
        )
    hypothesis = attribution.responsible.as_dict()

    if attribution.status.lower() not in _FAILURE_STATUSES:
        return _inconclusive(
            f"original run status is '{attribution.status}', not a failure; "
            "there is no failure outcome to flip",
            hypothesis=hypothesis,
        )

    manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text()) or {}
    model_calls_ref = str(manifest.get("model_calls_ref", "model-calls.jsonl"))
    model_calls = _read_jsonl(capsule_dir / model_calls_ref)

    try:
        payload = synthesize_intervention_payload(
            attribution.responsible, model_calls
        )
    except UnmappableHypothesisError as exc:
        return _inconclusive(str(exc), hypothesis=hypothesis)

    spec = InterventionSpec.model_validate(payload)
    intervention_meta: dict[str, Any] = {
        **spec.describe(),
        "payload": payload["mutate_payload"],
    }

    with tempfile.TemporaryDirectory(prefix="nf_diagnose_intervene_") as tmp:
        spec_path = Path(tmp) / "intervention.yaml"
        spec_path.write_text(yaml.dump(payload, allow_unicode=True))
        flags = ReplayFlags(mode="intervention", intervention_file=spec_path)
        engine = ReplayEngine(
            capsule_dir=capsule_dir, flags=flags, base_dir=base_dir
        )
        try:
            result = engine.run()
        except InterventionError as exc:
            return _inconclusive(
                f"intervention replay failed: {exc}",
                hypothesis=hypothesis,
                intervention=intervention_meta,
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
            hypothesis=hypothesis,
            intervention=intervention_meta,
            counterfactual=counterfactual,
            intervened=intervened,
        )
    if result.exit_code is None:
        return _inconclusive(
            "capsule has no re-executable command; the outcome flip is not "
            "measurable under mocked semantics",
            hypothesis=hypothesis,
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
    )
