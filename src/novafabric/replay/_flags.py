from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_LADDER: list[str] = [
    "none",
    "read-only",
    "idempotent-write",
    "non-idempotent-write",
    "external-side-effect",
    "unknown",
]

_ALLOW_READONLY_CLASSES = {"none", "read-only"}
_ALLOW_MUTATING_CLASSES = _ALLOW_READONLY_CLASSES | {"idempotent-write", "non-idempotent-write"}
_ALLOW_EXTERNAL_CLASSES = _ALLOW_MUTATING_CLASSES | {"external-side-effect"}
_ALLOW_UNKNOWN_CLASSES = _ALLOW_EXTERNAL_CLASSES | {"unknown"}

#: The operator flag that permits each mutation class (ADR-0012 safety ladder).
LADDER_FLAG: dict[str, str] = {
    "none": "always permitted",
    "read-only": "--allow-readonly",
    "idempotent-write": "--allow-mutating",
    "non-idempotent-write": "--allow-mutating",
    "external-side-effect": "--allow-external-side-effects",
    "unknown": "--allow-unknown-mutation",
}


def ladder_flag(mutation_class: str) -> str:
    """The flag an operator passes to permit *mutation_class* (unknown classes
    are treated as ``unknown``)."""
    return LADDER_FLAG.get(mutation_class, LADDER_FLAG["unknown"])


@dataclass
class ReplayFlags:
    mode: Literal["mocked", "forensic", "semantic", "exact", "intervention"] = "mocked"
    dry_run: bool = False
    allow_readonly: bool = False
    allow_mutating: bool = False
    allow_external_side_effects: bool = False
    allow_unknown_mutation: bool = False
    output_dir: Path | None = None
    # ADR-0086 — intervention mode spec file (experimental)
    intervention_file: Path | None = None
    # ADR-0126 — opt-in: refuse unless the capsule recorded this deployment_environment
    required_environment: str | None = None
    # ADR-0300 — opt-in escape hatch for `mocked` mode. Default (False) is
    # fail-closed: a model call with no recorded response, an unsupported model
    # surface, an unmatched MCP tool call, or a recorded response left
    # unconsumed fails the replay. True keeps the pre-0300 behaviour (empty
    # response + warning) and only records the divergences; an unmatched tool
    # call runs live only if `permits` its class, never against a replay.yaml
    # `allow: false` (ADR-0306 D7/D8).
    permissive: bool = False

    @property
    def divergence_policy(self) -> Literal["fail", "warn"]:
        return "warn" if self.permissive else "fail"

    def permits(self, mutation_class: str) -> bool:
        if self.allow_unknown_mutation:
            return mutation_class in _ALLOW_UNKNOWN_CLASSES
        if self.allow_external_side_effects:
            return mutation_class in _ALLOW_EXTERNAL_CLASSES
        if self.allow_mutating:
            return mutation_class in _ALLOW_MUTATING_CLASSES
        if self.allow_readonly:
            return mutation_class in _ALLOW_READONLY_CLASSES
        return mutation_class == "none"

    def active_flag_names(self) -> list[str]:
        flags = []
        if self.dry_run:
            flags.append("--dry-run")
        if self.allow_readonly:
            flags.append("--allow-readonly")
        if self.allow_mutating:
            flags.append("--allow-mutating")
        if self.allow_external_side_effects:
            flags.append("--allow-external-side-effects")
        if self.allow_unknown_mutation:
            flags.append("--allow-unknown-mutation")
        if self.permissive:
            flags.append("--permissive")
        # No implicit entry: a "--mock-tools" placeholder used to be recorded here,
        # but no such flag ever existed (ADR-0261). Tool substitution in mocked
        # mode (ADR-0300) is reported by the result's counters, not by a flag.
        return flags
