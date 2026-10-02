"""Opt-in deployment-environment gate for replay (ADR-0126 replay-gate wiring).

``nova replay --environment ENV`` (and ``ReplayFlags.required_environment``
for SDK callers) admits a replay only when the capsule recorded ``ENV`` as its
typed ``deployment_environment`` at capture. The check is record-only: it uses
the same reader as the ADR-0019 policy input, so a missing, malformed, or
rule-violating value is never inferred — and an unrecorded environment is
refused, not admitted (fail closed).

The refusal happens before anything runs: no policy evaluation, no replay
directory, no subprocess. :data:`EXIT_ENVIRONMENT_MISMATCH` (2) matches
``nova diff --environment``: "the requested comparison cannot be made" is
distinct from "the replay ran and failed" (1).
"""

from __future__ import annotations

from pathlib import Path

from novafabric.capture.deployment_env import ENVIRONMENT_VALUE_PATTERN
from novafabric.policy._environment import deployment_environment_from_capsule

#: Exit code for a replay refused by ``--environment`` (same as ``nova diff``).
EXIT_ENVIRONMENT_MISMATCH = 2


class ReplayEnvironmentMismatchError(Exception):
    """The capsule did not record the deployment environment the replay requires."""

    def __init__(self, capsule_dir: Path, required: str, recorded: str | None) -> None:
        self.capsule_dir = capsule_dir
        self.required = required
        self.recorded = recorded
        shown = repr(recorded) if recorded is not None else "no deployment_environment"
        super().__init__(
            f"replay refused: --environment {required} required, but {capsule_dir} "
            f"recorded {shown} (ADR-0126)"
        )


def validate_environment_value(value: str) -> str:
    """Return ``value`` if it satisfies the ADR-0126 value rule, else raise ``ValueError``."""
    if not ENVIRONMENT_VALUE_PATTERN.match(value):
        raise ValueError(
            f"invalid environment {value!r}: must match "
            f"{ENVIRONMENT_VALUE_PATTERN.pattern} (ADR-0126 value rule)"
        )
    return value


def check_replay_environment(capsule_dir: Path, required: str) -> str:
    """Return the recorded environment if it equals ``required``; raise otherwise.

    Comparison is exact (case-sensitive), matching ``nova diff --environment``:
    the value is an operator-chosen tag and is never normalised.

    Raises:
        ValueError: ``required`` violates the ADR-0126 value rule.
        ReplayEnvironmentMismatchError: the capsule recorded another value or none.
    """
    validate_environment_value(required)
    recorded = deployment_environment_from_capsule(capsule_dir)
    if recorded != required:
        raise ReplayEnvironmentMismatchError(Path(capsule_dir), required, recorded)
    return recorded
