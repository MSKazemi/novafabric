from __future__ import annotations

import shutil
import warnings

from ._budget import budget_block_from_capsule
from ._engine import PolicyEngine
from ._models import PolicyDecision, PolicyDeniedError, PolicyInput, PolicyResource, PolicySubject
from ._noop_engine import NoopEngine
from ._opa_engine import OpaEngine, OpaNotFoundError


def get_policy_engine() -> PolicyEngine:
    """Return the best available policy engine.

    If the ``opa`` binary is on PATH, returns an :class:`OpaEngine`.
    Otherwise emits a :class:`UserWarning` and returns a :class:`NoopEngine`
    that allows all requests.
    """
    if shutil.which("opa"):
        return OpaEngine()
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
    "NoopEngine",
    "OpaEngine",
    "OpaNotFoundError",
    "budget_block_from_capsule",
    "get_policy_engine",
]
