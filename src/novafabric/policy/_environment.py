"""Deployment environment for the policy input document (ADR-0126 P3).

Reads the typed ``deployment_environment`` field a capsule recorded at capture
(ADR-0126 P1) so an ADR-0019 Rego gate can condition on it as
``input.resource.deployment_environment`` — e.g. a production-only signing or
redaction requirement.

Record-only honesty: the value is passed **verbatim** from ``capsule.yaml``
and never inferred. No recorded value (or an unreadable / malformed manifest)
yields ``None`` — surfaced to Rego as ``null`` — never a fabricated default.
Only the typed top-level field is consulted, not free-form ``metadata``: the
typed field is authoritative for policy (capsule-environment-v0 §Edge cases).
A value violating the ADR-0126 value rule is dropped (``None``) rather than
handed to a gate, and a malformed manifest never raises: the gate itself
decides how to treat an absent environment (fail-safe, not fail-crash).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from novafabric.capture.deployment_env import ENVIRONMENT_VALUE_PATTERN

logger = logging.getLogger(__name__)

#: Capsule manifest file holding the top-level ``deployment_environment``.
CAPSULE_MANIFEST_FILE = "capsule.yaml"


def deployment_environment_from_capsule(capsule_dir: str | Path) -> str | None:
    """Return the capsule's recorded ``deployment_environment``, or ``None``.

    ``None`` when the manifest is missing, unreadable, not a mapping, carries
    no ``deployment_environment``, or carries one outside the ADR-0126 value
    rule (``^[A-Za-z0-9._:-]{1,64}$``). Never raises.
    """
    manifest_path = Path(capsule_dir) / CAPSULE_MANIFEST_FILE
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        logger.debug("no readable capsule manifest at %s: %s", manifest_path, exc)
        return None
    if not isinstance(manifest, dict):
        return None
    value = manifest.get("deployment_environment")
    if not isinstance(value, str) or not ENVIRONMENT_VALUE_PATTERN.match(value):
        if value is not None:
            logger.warning(
                "ignoring invalid deployment_environment %r in %s for policy input",
                value,
                manifest_path,
            )
        return None
    return value
