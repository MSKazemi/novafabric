from __future__ import annotations

import uuid

from ._models import PolicyDecision, PolicyInput


class NoopEngine:
    def evaluate(self, input_: PolicyInput) -> PolicyDecision:
        return PolicyDecision(
            allow=True,
            reason="noop-engine: opa not configured",
            decision_id=str(uuid.uuid4()),
        )
