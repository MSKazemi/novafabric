"""Safety-case verifier (ADR-0095, slice C1).

``verify_safety_case(case, capsule_dir)`` re-checks a compiled case against the
capsule it was built from:

1. **Artifact integrity.** Every :class:`Evidence` ``artifact_ref`` that points
   inside the bundle (``path_in_bundle`` not null) is re-hashed and compared to its
   recorded ``sha256``. External refs (``path_in_bundle`` null) are skipped — they
   are not in the bundle to re-hash.
2. **case_hash.** Recompute the canonical digest and compare it to the stored
   ``case_hash`` — a mutated body is detected.
3. **Invariant I1 — no naked claims.** A claim that is ``SUPPORTED``/``CONTESTED``/
   ``UNKNOWN`` must reference at least one child or evidence node. A claim with no
   support and no ``UNSUPPORTED`` flag is a naked claim and fails.

``verify_exit_code`` maps a verdict to a process exit code for CLI use.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from novafabric.safetycase.models import (
    BackingState,
    Claim,
    Evidence,
    SafetyCase,
)


@dataclass
class Verdict:
    """The outcome of verifying a safety case."""

    case_hash_ok: bool = True
    i1_ok: bool = True
    artifact_failures: list[str] = field(default_factory=list)
    i1_failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.case_hash_ok and self.i1_ok and not self.artifact_failures

    def summary(self) -> str:
        if self.ok:
            return "safety case OK: artifacts verified, case_hash matches, I1 holds"
        parts: list[str] = []
        if not self.case_hash_ok:
            parts.append("case_hash mismatch (body tampered)")
        if self.artifact_failures:
            parts.append(
                f"{len(self.artifact_failures)} artifact failure(s): "
                + "; ".join(self.artifact_failures)
            )
        if not self.i1_ok:
            parts.append("invariant I1 violated: " + "; ".join(self.i1_failures))
        return "safety case FAILED: " + " | ".join(parts)


def verify_safety_case(case: SafetyCase, capsule_dir: Path) -> Verdict:
    """Re-verify a compiled safety case against its capsule."""
    verdict = Verdict()

    # 1. recompute case_hash over the canonical body
    verdict.case_hash_ok = case.case_hash == case.compute_case_hash()

    # 2. re-hash each in-bundle evidence artifact
    for node in case.nodes:
        if not isinstance(node, Evidence):
            continue
        ref = node.artifact_ref
        if ref.path_in_bundle is None:
            continue  # external artifact — not in the bundle to re-hash
        path = capsule_dir / ref.path_in_bundle
        if not path.exists():
            verdict.artifact_failures.append(
                f"{node.id}: missing artifact {ref.path_in_bundle}"
            )
            continue
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != ref.sha256:
            verdict.artifact_failures.append(
                f"{node.id}: digest mismatch for {ref.path_in_bundle}"
            )

    # 3. invariant I1 — no naked claims
    for node in case.nodes:
        if not isinstance(node, Claim):
            continue
        supported_states = {
            BackingState.SUPPORTED,
            BackingState.CONTESTED,
            BackingState.UNKNOWN,
        }
        if node.backing_state in supported_states and not (
            node.children or node.evidence_ids
        ):
            verdict.i1_failures.append(
                f"{node.id}: {node.backing_state.value} claim with no children "
                "and no evidence (naked claim)"
            )
    verdict.i1_ok = not verdict.i1_failures

    return verdict


def verify_exit_code(verdict: Verdict) -> int:
    """Map a verdict to a process exit code.

    0 — OK; 3 — case_hash mismatch; 4 — artifact failure; 5 — I1 violation.
    The first failing check (in that order) determines the code.
    """
    if verdict.ok:
        return 0
    if not verdict.case_hash_ok:
        return 3
    if verdict.artifact_failures:
        return 4
    return 5
