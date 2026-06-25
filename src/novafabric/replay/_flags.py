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
        if not flags and not self.dry_run:
            flags.append("--mock-tools")
        return flags
