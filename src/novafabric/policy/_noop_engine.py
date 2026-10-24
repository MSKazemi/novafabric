from __future__ import annotations

import uuid

from ._models import PolicyDecision, PolicyInput


class NoopEngine:
    """Allows everything: local mode with no ``opa`` and no configured policy bundle."""

    def evaluate(self, input_: PolicyInput) -> PolicyDecision:
        return PolicyDecision(
            allow=True,
            reason="noop-engine: opa not configured",
            decision_id=str(uuid.uuid4()),
        )


class FailClosedEngine:
    """Denies everything, with the reason: a policy was configured but cannot be evaluated.

    Returned by :func:`novafabric.policy.get_policy_engine` when
    ``NOVAFABRIC_POLICY_BUNDLE_PATH`` names the operator's policies but no ``opa``
    binary is on PATH (review X4). A missing binary must not silently open every
    gate those policies close. Every refusal is still a decision with an id, so
    it is audited like any other deny.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def evaluate(self, input_: PolicyInput) -> PolicyDecision:
        return PolicyDecision(
            allow=False,
            reason=self._reason,
            decision_id=str(uuid.uuid4()),
        )
