"""Tests for policy model round-trips and NoopEngine (Track P-1, v0.8)."""
from __future__ import annotations

from novafabric.policy import (
    NoopEngine,
    PolicyDecision,
    PolicyInput,
    PolicyResource,
    PolicySubject,
)


def test_policy_input_roundtrip() -> None:
    """PolicyInput serialises and re-validates without data loss."""
    inp = PolicyInput(
        action="promote",
        subject=PolicySubject(user="alice", roles=["admin"]),
        resource=PolicyResource(
            kind="capsule",
            ref="capsule-001",
            eval_score=0.95,
            unsafe_skips=0,
        ),
        context={"env": "staging"},
    )
    raw = inp.model_dump_json()
    restored = PolicyInput.model_validate_json(raw)

    assert restored.action == "promote"
    assert restored.subject.user == "alice"
    assert restored.subject.roles == ["admin"]
    assert restored.resource.kind == "capsule"
    assert restored.resource.eval_score == 0.95
    assert restored.resource.unsafe_skips == 0
    assert restored.context == {"env": "staging"}


def test_policy_decision_deny() -> None:
    """A deny decision is correctly represented and its allow field is False."""
    decision = PolicyDecision(allow=False, reason="eval score below threshold")
    assert decision.allow is False
    assert decision.reason == "eval score below threshold"


def test_noop_engine_always_allows() -> None:
    """NoopEngine.evaluate must always return allow=True regardless of input."""
    engine = NoopEngine()
    inp = PolicyInput(
        action="promote",
        subject=PolicySubject(user="nobody", roles=[]),
        resource=PolicyResource(
            kind="asset",
            ref="asset-001",
            eval_score=0.0,
            unsafe_skips=99,
        ),
    )
    decision = engine.evaluate(inp)
    assert decision.allow is True
    assert decision.decision_id != ""
    assert "noop" in decision.reason
