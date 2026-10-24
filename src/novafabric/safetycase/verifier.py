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
   support and no ``UNSUPPORTED`` flag is a naked claim and fails. Every reference
   must resolve: ``evidence_ids`` to Evidence nodes, ``children`` to claims or
   argument strategies, ``top_claim_id`` to a claim (review misc-1-03).
4. **Subject.** The case must describe *this* capsule: ``subject.capsule_hash`` is
   the SHA-256 of its ``capsule.yaml`` now, and a capsule directory named by a ULID
   is among ``subject.run_ids``. ``case_hash`` is an unkeyed self-hash anyone can
   recompute, so without this a case built for — or forged against — another
   capsule verified (review misc-1-03).

Artifact paths come from the case file, which is untrusted: each is read through
the capsule's no-follow reader, so an absolute path, a ``..`` or a link is refused,
and hashed in chunks (review misc-1-04).

``verify_exit_code`` maps a verdict to a process exit code for CLI use.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from novafabric._capsule_fs import CapsuleEntryError, open_capsule_file, read_capsule_file
from novafabric.capsule.ulid_util import is_valid_ulid
from novafabric.safetycase.models import (
    ArgumentStrategy,
    BackingState,
    Claim,
    Evidence,
    SafetyCase,
)

_OPERATION = "nova safety-case verify"
_CHUNK = 1 << 20


@dataclass
class Verdict:
    """The outcome of verifying a safety case."""

    case_hash_ok: bool = True
    i1_ok: bool = True
    artifact_failures: list[str] = field(default_factory=list)
    i1_failures: list[str] = field(default_factory=list)
    subject_failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.case_hash_ok
            and self.i1_ok
            and not self.artifact_failures
            and not self.subject_failures
        )

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
        if self.subject_failures:
            parts.append("subject mismatch: " + "; ".join(self.subject_failures))
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
        try:
            actual = _hash_capsule_file(capsule_dir, ref.path_in_bundle)
        except FileNotFoundError:
            verdict.artifact_failures.append(
                f"{node.id}: missing artifact {ref.path_in_bundle}"
            )
            continue
        except (ValueError, CapsuleEntryError, OSError) as exc:
            verdict.artifact_failures.append(
                f"{node.id}: artifact path {ref.path_in_bundle!r} refused: {exc}"
            )
            continue
        if actual != ref.sha256:
            verdict.artifact_failures.append(
                f"{node.id}: digest mismatch for {ref.path_in_bundle}"
            )

    # 3. invariant I1 — no naked claims, and every reference resolves
    evidence_ids = {n.id for n in case.nodes if isinstance(n, Evidence)}
    structural_ids = {n.id for n in case.nodes if isinstance(n, (Claim, ArgumentStrategy))}
    claim_ids = {n.id for n in case.nodes if isinstance(n, Claim)}
    if case.top_claim_id not in claim_ids:
        verdict.i1_failures.append(f"top_claim_id {case.top_claim_id!r} names no claim")
    for node in case.nodes:
        if not isinstance(node, Claim):
            continue
        for ref_id in node.evidence_ids:
            if ref_id not in evidence_ids:
                verdict.i1_failures.append(f"{node.id}: evidence {ref_id!r} does not exist")
        for ref_id in node.children:
            if ref_id not in structural_ids:
                verdict.i1_failures.append(f"{node.id}: child {ref_id!r} does not exist")
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

    # 4. the case describes this capsule
    verdict.subject_failures = _subject_failures(case, capsule_dir)

    return verdict


def _hash_capsule_file(capsule_dir: Path, rel: str) -> str:
    """``sha256:<hex>`` of capsule file *rel*, read no-follow and in bounded chunks."""
    digest = hashlib.sha256()
    with open_capsule_file(capsule_dir, rel, operation=_OPERATION) as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _subject_failures(case: SafetyCase, capsule_dir: Path) -> list[str]:
    failures: list[str] = []
    try:
        manifest = read_capsule_file(capsule_dir, "capsule.yaml", operation=_OPERATION)
    except FileNotFoundError:
        manifest = b""  # the compiler hashes an absent manifest as empty, too
    except (ValueError, CapsuleEntryError, OSError) as exc:
        return [f"capsule.yaml not read: {exc}"]
    current = "sha256:" + hashlib.sha256(manifest).hexdigest()
    if case.subject.capsule_hash != current:
        failures.append(
            "subject.capsule_hash is not this capsule's capsule.yaml — the case was "
            "built for another capsule, or the capsule changed since"
        )
    if is_valid_ulid(capsule_dir.name) and capsule_dir.name not in case.subject.run_ids:
        failures.append(f"run {capsule_dir.name} is not among subject.run_ids")
    return failures


def verify_exit_code(verdict: Verdict) -> int:
    """Map a verdict to a process exit code.

    0 — OK; 3 — case_hash mismatch; 4 — artifact failure; 5 — I1 violation;
    6 — the case's subject is not this capsule. The first failing check (in that
    order) determines the code.
    """
    if verdict.ok:
        return 0
    if not verdict.case_hash_ok:
        return 3
    if verdict.artifact_failures:
        return 4
    if not verdict.i1_ok:
        return 5
    return 6
