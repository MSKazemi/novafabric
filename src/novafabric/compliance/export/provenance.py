# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""`evidence_source` provenance marker (ADR-0197).

Distinguishes, in the compliance-export envelope, a value NovaFabric verified
against a sealed capsule from a value an operator merely asserted, and from one
NovaFabric tried and failed to verify.

Invariants (ADR-0197):

* **I-1 No silent provenance** — every exported field-group is attributable.
* **I-2 Failure is visible** — an attempted-and-failed verification is reported
  as ``unverifiable``, never downgraded to ``operator_asserted``.
* **I-3 Re-performable** — ``capsule_verified`` carries an
  :class:`EvidenceSourceRef` a third party can re-check offline.
* **I-4 NovaFabric does not adjudicate** — the marker records *how* a value was
  established, never whether the underlying compliance claim is true.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .models import CompletenessSummaryEntry

# Capsule files hashed by default into a re-performable reference. The manifest
# is the anchor; the event streams are included when present so a third party
# re-hashing the same files reproduces the digest.
_DEFAULT_REF_FILES: tuple[str, ...] = (
    "capsule.yaml",
    "tool-permission-events.jsonl",
    "model-calls.jsonl",
    "tool-calls.jsonl",
)


class EvidenceSource(str, Enum):
    """How a compliance-export field-group value was established (ADR-0197 D1)."""

    operator_asserted = "operator_asserted"
    """The value came from operator-authored input; NovaFabric did not check it."""

    capsule_verified = "capsule_verified"
    """NovaFabric resolved this from a sealed capsule and re-performed the binding."""

    unverifiable = "unverifiable"
    """NovaFabric attempted verification and could not complete it (missing
    capsule, unresolvable digest, or absent seal)."""


class EvidenceSourceRef(BaseModel):
    """Re-performable reference backing a ``capsule_verified`` marker (I-3).

    Carries exactly what a third party needs to re-check the binding offline via
    the existing ``nova verify`` / ``verify_envelope`` path.
    """

    capsule_id: str = Field(..., description="Capsule the value was resolved from")
    content_digest: str = Field(
        ...,
        description="Digest of the resolved artifact, e.g. 'sha256:...'",
    )
    seal_envelope_path: str | None = Field(
        default=None,
        description="Optional path to the NovaSeal envelope proving the seal",
    )
    verified_at: str = Field(
        ...,
        description="ISO 8601 UTC timestamp the verification was performed",
    )


class ProvenanceError(ValueError):
    """Base class for evidence_source provenance failures."""


class UnmarkedFieldGroupError(ProvenanceError):
    """A field-group carries no ``evidence_source`` where one is required (I-1)."""


class MissingReperformableRefError(ProvenanceError):
    """A ``capsule_verified`` marker lacks its re-performable reference (I-3)."""


def build_capsule_ref(
    capsule_dir: Path,
    verified_at: str,
    *,
    files: Sequence[str] | None = None,
    seal_envelope_path: str | None = None,
) -> EvidenceSourceRef | None:
    """Build a re-performable :class:`EvidenceSourceRef` for *capsule_dir* (I-3).

    The digest covers the capsule manifest and, when present, the event streams a
    third party can re-hash offline. Returns ``None`` when no manifest is present,
    in which case no field may be honestly marked ``capsule_verified``.
    """
    import yaml  # local import: yaml is only needed on the capsule-verified path

    manifest_path = capsule_dir / "capsule.yaml"
    if not manifest_path.exists():
        return None
    h = hashlib.sha256()
    for name in files or _DEFAULT_REF_FILES:
        p = capsule_dir / name
        if p.exists():
            h.update(name.encode())
            h.update(p.read_bytes())
    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    capsule_id = (
        manifest.get("capsule_id") or manifest.get("run_id") or capsule_dir.name
    )
    return EvidenceSourceRef(
        capsule_id=str(capsule_id),
        content_digest="sha256:" + h.hexdigest(),
        seal_envelope_path=seal_envelope_path,
        verified_at=verified_at,
    )


def mark(
    source: EvidenceSource,
    *,
    ref: EvidenceSourceRef | None = None,
) -> tuple[EvidenceSource, EvidenceSourceRef | None]:
    """Validate a (source, ref) pairing and return it.

    ``capsule_verified`` requires a :class:`EvidenceSourceRef` (I-3); the other
    two values must not carry one, since a ref would misrepresent an assertion or
    a failed verification as a re-performable check.

    Raises:
        MissingReperformableRefError: ``capsule_verified`` with no ``ref``.
        ProvenanceError: a non-verified source carrying a stray ``ref``.
    """
    if source is EvidenceSource.capsule_verified:
        if ref is None:
            raise MissingReperformableRefError(
                "capsule_verified requires an EvidenceSourceRef so the binding "
                "can be re-performed offline (ADR-0197 I-3)"
            )
        return source, ref
    if ref is not None:
        raise ProvenanceError(
            f"{source.value} must not carry an EvidenceSourceRef; a reference "
            "would misrepresent it as a re-performable verification (ADR-0197 I-2)"
        )
    return source, None


def validate_marked(
    entries: Iterable[CompletenessSummaryEntry],
    *,
    require: bool = True,
) -> None:
    """Assert a set of field-group entries is honestly marked (ADR-0197 I-1/I-3).

    Args:
        entries: the field-group records to check.
        require: when ``True`` (the export-path default), every entry must carry
            an ``evidence_source``; when ``False``, unmarked entries are tolerated
            (used for backward-compatible reads of pre-ADR-0197 envelopes).

    Raises:
        UnmarkedFieldGroupError: an entry has no ``evidence_source`` and
            ``require`` is True.
        MissingReperformableRefError: a ``capsule_verified`` entry has no
            ``evidence_ref``.
    """
    for entry in entries:
        source = entry.evidence_source
        if source is None:
            if require:
                raise UnmarkedFieldGroupError(
                    f"field-group {entry.field_name!r} has no evidence_source "
                    "(ADR-0197 I-1: no silent provenance)"
                )
            continue
        if source is EvidenceSource.capsule_verified and entry.evidence_ref is None:
            raise MissingReperformableRefError(
                f"field-group {entry.field_name!r} is capsule_verified but carries "
                "no evidence_ref (ADR-0197 I-3)"
            )


__all__ = [
    "EvidenceSource",
    "EvidenceSourceRef",
    "MissingReperformableRefError",
    "ProvenanceError",
    "UnmarkedFieldGroupError",
    "build_capsule_ref",
    "mark",
    "validate_marked",
]
