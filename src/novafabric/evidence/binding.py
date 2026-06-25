"""Criterion→evidence bindings (ADR-0087(b), gap-008).

A :class:`CriterionBinding` maps a compliance criterion ID (the control IDs
from the audit profiles under ``compliance/audit/profiles/``) to the specific
capsule facts that evidence it — file path + sha256 digest + optional
JSONPath (RFC 9535 dialect; path + digest alone is already a valid binding).

A verifier recomputes each digest against the bundle and re-evaluates the
binding with no NovaFabric dependency (ADR-0011 portability commitment).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from novafabric.compliance.audit.loader import load_profile
from novafabric.compliance.audit.models import ControlProfile
from novafabric.evidence.intoto import dsse_sign, make_intoto_statement

BINDING_PREDICATE_TYPE = "https://novafabric.io/criterion-binding/v0"

#: evidence_type → capsule-relative file evidencing it (mirrors the audit
#: engine's checkers; a binding is only emitted when the file exists)
_EVIDENCE_FILES: dict[str, str] = {
    "model_calls": "model-calls.jsonl",
    "tool_permissions": "tool-permission-events.jsonl",
    "policy_events": "tool-permission-events.jsonl",
    "seal_signature": "seal-bundle.json",
    "lineage": "lineage.jsonl",
    "pii_scan": "redaction-proof.json",
}


class EvidenceRef(BaseModel):
    """One capsule fact: path within the capsule + digest (+ optional path)."""

    path: str
    sha256: str
    jsonpath: str | None = Field(
        default=None, description="Optional RFC 9535 JSONPath into the file"
    )


class CriterionBinding(BaseModel):
    """Machine-checkable criterion→evidence traceability (ADR-0087(b))."""

    schema_version: str = "0.1.0"
    criterion_id: str
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    binding_status: Literal["satisfied", "partial", "unsatisfied"]


def _ref_for(capsule_dir: Path, rel_path: str) -> EvidenceRef | None:
    path = capsule_dir / rel_path
    if not path.exists():
        return None
    return EvidenceRef(
        path=rel_path, sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )


def build_bindings(
    capsule_dir: Path, profile: ControlProfile | str
) -> list[CriterionBinding]:
    """Generate bindings for every control in *profile* against one capsule.

    Status mirrors ``ControlCoverage`` semantics: all evidence present →
    ``satisfied``; some → ``partial``; none → ``unsatisfied`` (with an empty
    ref list — the absence itself is the auditable claim).
    """
    resolved = (
        load_profile(profile) if isinstance(profile, str) else profile
    )
    bindings: list[CriterionBinding] = []
    for control in resolved.controls:
        refs: list[EvidenceRef] = []
        wanted = 0
        for req in control.evidence_requirements:
            rel = _EVIDENCE_FILES.get(req.evidence_type)
            if rel is None:
                continue
            wanted += 1
            ref = _ref_for(capsule_dir, rel)
            if ref is not None:
                refs.append(ref)
        if wanted and len(refs) == wanted:
            status: Literal["satisfied", "partial", "unsatisfied"] = "satisfied"
        elif refs:
            status = "partial"
        else:
            status = "unsatisfied"
        bindings.append(
            CriterionBinding(
                criterion_id=control.id,
                evidence_refs=refs,
                binding_status=status,
            )
        )
    return bindings


def verify_binding(capsule_dir: Path, binding: CriterionBinding) -> bool:
    """Re-check a binding: every evidence ref's digest must still match."""
    for ref in binding.evidence_refs:
        path = capsule_dir / ref.path
        if not path.exists():
            return False
        if hashlib.sha256(path.read_bytes()).hexdigest() != ref.sha256:
            return False
    return True


def attest_bindings(
    capsule_dir: Path,
    run_id: str,
    bindings: list[CriterionBinding],
    signer: Any,
) -> dict[str, Any]:
    """Wrap bindings in an in-toto statement and DSSE-sign them."""
    manifest_path = capsule_dir / "capsule.yaml"
    digest = hashlib.sha256(
        manifest_path.read_bytes() if manifest_path.exists() else b""
    ).hexdigest()
    statement = make_intoto_statement(
        predicate_type=BINDING_PREDICATE_TYPE,
        subject_name=f"run-capsule/{run_id}",
        subject_sha256=digest,
        predicate={"bindings": [b.model_dump() for b in bindings]},
    )
    envelope: dict[str, Any] = dsse_sign(statement, signer)
    return envelope
