"""Citizen-facing decision-explanation export (ADR-0169 D1 / NF-377, first slice).

A pure exporter that assembles a plain-language, **subject-facing** record of *meaningful
information* about an automated decision (GDPR Art. 22 + Arts. 13–15 shape):

* ``decision_ref`` — the sealed decision the explanation is about,
* ``factors`` — the recorded, **non-secret** signals that entered the decision,
* ``human_involvement`` — ``solely_automated`` / ``human_in_the_loop`` / ``human_reviewed``,
* ``contest_channel_ref`` — how the subject contests the decision,
* ``logic_summary_ref`` — a reference to a recorded, **non-proprietary** logic summary.

Two honesty constraints from the ADR are enforced here. (1) It records meaningful information but
**never claims the explanation is legally sufficient** — there is deliberately no
legal-sufficiency / verdict field. (2) It **MUST NOT expose model internals** (weights, logits,
activations, embeddings, …) or raw sensitive identifiers: a validator rejects any ``factor`` whose
text matches such a shape.

Honest limitation: whether a factor names *another* subject is a determination this exporter cannot
make from the text alone — that adjudication stays with the collector / a qualified human. This
slice enforces the model-internals and raw-identifier exclusions, and carries only what it is given.
"""
from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from pydantic import BaseModel


class HumanInvolvement(str, Enum):
    solely_automated = "solely_automated"
    human_in_the_loop = "human_in_the_loop"
    human_reviewed = "human_reviewed"


#: Substrings marking a factor as model internals or a raw sensitive identifier (lowercased match).
#: Curated so legitimate plain-language factors do not match; anything matching here is rejected.
DISALLOWED_FACTOR_PATTERNS: tuple[str, ...] = (
    # model internals
    "weight",
    "gradient",
    "logit",
    "activation",
    "embedding",
    "hyperparameter",
    "neuron",
    "softmax",
    "hidden_state",
    "attention_head",
    "layer_",
    "checkpoint_step",
    # raw sensitive identifiers (compound tokens that never belong in a plain-language factor)
    "ssn",
    "social_security",
    "passport_number",
    "credit_card",
    "national_id",
)


class CitizenExplanation(BaseModel):
    decision_ref: str
    factors: list[str]  # recorded non-secret signals — no model internals, no raw identifiers
    human_involvement: str  # a HumanInvolvement value
    contest_channel_ref: str | None = None
    logic_summary_ref: str | None = None  # ref to a recorded non-proprietary logic summary
    # Intentionally NO legal-sufficiency / verdict field — never claims the explanation suffices.


def disallowed_factor_content(factors: Sequence[str]) -> list[str]:
    """Return the factors whose text matches a model-internals / raw-identifier shape."""
    return [
        f
        for f in factors
        if any(pat in f.lower() for pat in DISALLOWED_FACTOR_PATTERNS)
    ]


def build_citizen_explanation(
    *,
    decision_ref: str | None = None,
    factors: Sequence[str] = (),
    human_involvement: str | None = None,
    contest_channel_ref: str | None = None,
    logic_summary_ref: str | None = None,
) -> CitizenExplanation:
    """Assemble a citizen decision-explanation record.

    Requires ``decision_ref`` and a valid ``human_involvement`` (one of the three
    :class:`HumanInvolvement` values). Rejects (``ValueError``) any ``factor`` that exposes model
    internals or a raw sensitive identifier — such content never enters a public explanation.
    """
    if not decision_ref:
        raise ValueError("decision_ref is required")
    if not human_involvement:
        raise ValueError("human_involvement is required")
    try:
        level = HumanInvolvement(human_involvement)
    except ValueError as exc:
        allowed = ", ".join(h.value for h in HumanInvolvement)
        raise ValueError(
            f"unknown human_involvement {human_involvement!r}; expected one of: {allowed}"
        ) from exc

    factor_list = list(factors)
    disallowed = disallowed_factor_content(factor_list)
    if disallowed:
        raise ValueError(
            "refusing to expose model internals or raw identifiers in a citizen explanation; "
            f"offending factor(s): {'; '.join(disallowed)}"
        )

    return CitizenExplanation(
        decision_ref=decision_ref,
        factors=factor_list,
        human_involvement=level.value,
        contest_channel_ref=contest_channel_ref,
        logic_summary_ref=logic_summary_ref,
    )
