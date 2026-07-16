"""Deployment-environment resolution for run capsules (ADR-0126).

Records *where in the delivery lifecycle* a run executed — ``production``,
``staging``, ``development``, ``test``, or any named custom environment.

This is **not** the environment lock. ``env.lock`` (ADR-0007) records the
technical software/hardware environment for reproducible replay;
``deployment_environment`` records the operator-chosen deployment context and
says nothing about the machine. The two are orthogonal.

NovaFabric **records** this value verbatim from an explicit source; it never
**infers** it (from hostname, namespace, git branch, or anything else).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: Environment variable consulted when no CLI flag is given (ADR-0126 §D2).
ENVIRONMENT_ENV_VAR = "NOVAFABRIC_ENVIRONMENT"

#: Recommended (not enforced) values; anything else warns but is accepted.
CONVENTIONAL_ENVIRONMENTS = frozenset({"production", "staging", "development", "test"})

#: Normative value rule from capsule-environment-v0: non-empty, bounded, safe charset.
ENVIRONMENT_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

EnvironmentSource = Literal["cli-flag", "env-var", "sdk-arg"]


class InvalidDeploymentEnvironmentError(ValueError):
    """An explicitly supplied deployment-environment value is invalid.

    Raised for explicit program input (CLI flag, SDK argument) so the operator
    gets immediate feedback *before* any capture starts. An invalid ambient
    ``NOVAFABRIC_ENVIRONMENT`` value never raises — it is warned about and
    dropped, because a bad shell variable must never block the user workload.
    """


class ResolvedDeploymentEnvironment(BaseModel):
    """A deployment-environment value together with its recorded provenance."""

    value: str
    source: EnvironmentSource


def unconventional_warning(value: str) -> str | None:
    """Return a warning message when *value* is outside the conventional set.

    The check is case-insensitive (``Production`` does not warn) per the spec;
    custom environments are legitimate, so this is advisory only — never an
    error. Returns ``None`` when the value is conventional.
    """
    if value.lower() in CONVENTIONAL_ENVIRONMENTS:
        return None
    conventional = ", ".join(sorted(CONVENTIONAL_ENVIRONMENTS))
    return (
        f"deployment_environment '{value}' is outside the conventional set "
        f"{{{conventional}}} — recorded verbatim (custom environments are "
        "legitimate; check for typos)"
    )


def _validate(value: str, source: EnvironmentSource) -> ResolvedDeploymentEnvironment | None:
    """Validate one candidate value; behavior depends on how explicit it was."""
    if ENVIRONMENT_VALUE_PATTERN.match(value):
        return ResolvedDeploymentEnvironment(value=value, source=source)
    if source == "env-var":
        # Ambient input: warn and drop — never block the workload (fail-open).
        logger.warning(
            "Ignoring invalid %s value %r: must match %s",
            ENVIRONMENT_ENV_VAR,
            value,
            ENVIRONMENT_VALUE_PATTERN.pattern,
        )
        return None
    # Explicit input (CLI flag / SDK argument): fail fast before capture.
    raise InvalidDeploymentEnvironmentError(
        f"invalid deployment environment {value!r} (from {source}): "
        f"must match {ENVIRONMENT_VALUE_PATTERN.pattern}"
    )


def resolve_deployment_environment(
    cli_value: str | None = None,
    sdk_value: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedDeploymentEnvironment | None:
    """Resolve the deployment environment from explicit sources only.

    Precedence (ADR-0126 §D2): CLI flag > ``NOVAFABRIC_ENVIRONMENT`` env var >
    SDK argument. An empty (or whitespace-only) value counts as *not supplied*
    and falls through to the next source; if no source supplies a value the
    result is ``None`` and the capsule fields stay absent.

    Raises:
        InvalidDeploymentEnvironmentError: The CLI or SDK value is non-empty
            but violates the value rule. An invalid env-var value is warned
            about and skipped instead (never blocks the workload).
    """
    env = os.environ if environ is None else environ
    candidates: list[tuple[str | None, EnvironmentSource]] = [
        (cli_value, "cli-flag"),
        (env.get(ENVIRONMENT_ENV_VAR), "env-var"),
        (sdk_value, "sdk-arg"),
    ]
    for candidate, source in candidates:
        if candidate is None or not candidate.strip():
            continue  # not supplied (empty normalizes to absent, per spec)
        resolved = _validate(candidate, source)
        if resolved is not None:
            return resolved
    return None
