from __future__ import annotations

import os
import shutil
import warnings

from ._budget import budget_block_from_capsule
from ._engine import PolicyEngine
from ._environment import deployment_environment_from_capsule
from ._models import PolicyDecision, PolicyDeniedError, PolicyInput, PolicyResource, PolicySubject
from ._noop_engine import FailClosedEngine, NoopEngine
from ._opa_engine import OpaEngine, OpaNotFoundError, OpaSandboxError


def get_policy_engine() -> PolicyEngine:
    """Return the best available policy engine.

    If the ``opa`` binary is on PATH, returns an :class:`OpaEngine`. Otherwise:

    * ``NOVAFABRIC_POLICY_BUNDLE_PATH`` is set (the operator configured their own
      policies) → a :class:`FailClosedEngine` that denies every gated action with
      the reason, and a :class:`UserWarning`. A missing binary never opens a gate
      a configured policy would close (review X4).
    * nothing is configured (the local-mode default; OPA is optional) → a
      :class:`NoopEngine` that allows all requests, and a :class:`UserWarning`.
    """
    if shutil.which("opa"):
        return OpaEngine()
    bundle = os.environ.get("NOVAFABRIC_POLICY_BUNDLE_PATH", "").strip()
    if bundle:
        reason = (
            f"NOVAFABRIC_POLICY_BUNDLE_PATH is set ({bundle}) but the opa binary was not "
            "found — policy gates fail closed. Install OPA or unset the variable: "
            "https://www.openpolicyagent.org/docs/latest/#running-opa"
        )
        warnings.warn(reason, stacklevel=2)
        return FailClosedEngine(reason)
    warnings.warn(
        "opa binary not found — policy gates disabled (NoopEngine). "
        "Install OPA: https://www.openpolicyagent.org/docs/latest/#running-opa",
        stacklevel=2,
    )
    return NoopEngine()


__all__ = [
    "PolicyEngine",
    "PolicyInput",
    "PolicyDecision",
    "PolicyDeniedError",
    "PolicySubject",
    "PolicyResource",
    "FailClosedEngine",
    "NoopEngine",
    "OpaEngine",
    "OpaNotFoundError",
    "OpaSandboxError",
    "budget_block_from_capsule",
    "deployment_environment_from_capsule",
    "get_policy_engine",
]
