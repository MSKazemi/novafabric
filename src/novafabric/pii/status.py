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
"""Read-only PII encryption/erasure status for a capsule (`nova pii status`, ADR-0069).

Correlates a capsule's ``redaction_manifest.json`` (subject HMACs, per-field
redaction records — see :mod:`novafabric.compliance.pii.manifest`) with the
per-subject DEK lifecycle in ``$NOVAFABRIC_HOME/dek.db``:

- ``active``  — a live DEK exists for the subject (ciphertext still recoverable
  by the key holder);
- ``erased``  — no DEK exists (crypto-shredded per ADR-0069, or ``dek.db`` is
  absent — per ADR-0069 loss of ``dek.db`` achieves the same erasure guarantee);
- ``unknown`` — ``dek.db`` is present but ``NOVA_PII_PEPPER`` is not available,
  so manifest HMACs cannot be correlated to stored subject IDs.

Invariants:

- Strictly read-only: never creates or modifies ``dek.db`` or any capsule file.
- Local-first: no server, no network.
- No key material (``dek_hex``) and no plaintext subject IDs appear in the report;
  subjects are identified only by their manifest HMAC.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from novafabric.compliance.pii.manifest import RedactionManifest
from novafabric.pii.dek import DEKStore, DEKSubjectRecord

_MANIFEST_FILENAME = "redaction_manifest.json"

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PIIStatusError(Exception):
    """Base error for PII status reporting."""


class CapsuleNotFoundError(PIIStatusError):
    """The capsule ID or path could not be resolved."""


class ManifestInvalidError(PIIStatusError):
    """A redaction_manifest.json exists but could not be parsed."""


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


class PIIFieldStatus(BaseModel):
    """One encrypted/redacted PII field recorded in the capsule's manifest."""

    field_path: str = Field(..., description="Dot-notation path of the protected field")
    detection_rule_id: str = Field(..., description="Detection rule that flagged the field")
    subject_id_hmac: str = Field(..., description="HMAC of the data subject ('sha256:<hex>')")
    redacted_at_utc: str = Field(..., description="ISO 8601 UTC timestamp of the redaction")

    model_config = {"frozen": True}


class SubjectDEKStatus(BaseModel):
    """Per-subject DEK lifecycle state for the capsule."""

    subject_id_hmac: str = Field(..., description="HMAC of the data subject ('sha256:<hex>')")
    dek_state: Literal["active", "erased", "unknown"] = Field(
        ...,
        description=(
            "'active' = live DEK found; 'erased' = no DEK (crypto-shredded or "
            "dek.db absent); 'unknown' = NOVA_PII_PEPPER unavailable, cannot correlate"
        ),
    )
    dek_created_at: datetime | None = Field(
        default=None,
        description="UTC creation time of the live DEK (active subjects only)",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Tenant namespace of the live DEK (active subjects only)",
    )
    field_count: int = Field(..., description="Number of manifest fields for this subject")
    detection_rule_ids: list[str] = Field(
        default_factory=list,
        description="Sorted unique detection rules that hit for this subject",
    )
    last_redacted_at_utc: str | None = Field(
        default=None,
        description="Latest redacted_at_utc among this subject's fields",
    )

    model_config = {"frozen": True}


class PIIStatusReport(BaseModel):
    """Complete read-only PII status report for a single capsule."""

    capsule_id: str = Field(..., description="Capsule identifier")
    capsule_dir: str | None = Field(
        default=None,
        description="Resolved capsule directory, if any",
    )
    manifest_present: bool = Field(
        ...,
        description="Whether redaction_manifest.json was found for the capsule",
    )
    encrypted_field_count: int = Field(
        ...,
        description="Total PII fields protected (manifest entries)",
    )
    dek_store_path: str = Field(..., description="Path of the DEK store consulted")
    dek_store_present: bool = Field(..., description="Whether dek.db exists")
    pepper_available: bool = Field(
        ...,
        description="Whether NOVA_PII_PEPPER was available for HMAC correlation",
    )
    subjects: list[SubjectDEKStatus] = Field(
        default_factory=list,
        description="Per-subject DEK state, ordered by subject HMAC",
    )
    fields: list[PIIFieldStatus] = Field(
        default_factory=list,
        description="All protected fields in manifest order",
    )
    generated_at: datetime = Field(..., description="UTC timestamp this report was built")

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _hmac_subject(pepper: bytes, subject_id: str) -> str:
    """Return ``sha256:<hex>`` HMAC of *subject_id* keyed by *pepper*.

    Must match the format written by
    :class:`novafabric.compliance.pii.gate.PIIDetectionGate`.
    """
    mac = hmac.new(pepper, subject_id.encode("utf-8"), hashlib.sha256)
    return "sha256:" + mac.hexdigest()


def _load_manifest(manifest_path: Path) -> RedactionManifest:
    """Parse a redaction manifest, raising :class:`ManifestInvalidError` on failure."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return RedactionManifest.model_validate(data)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise ManifestInvalidError(
            f"Cannot parse redaction manifest at {manifest_path}: {exc}"
        ) from exc


def _resolve_capsule(
    capsule: str,
    capsule_dir: Path,
) -> tuple[str, Path | None, Path | None]:
    """Resolve *capsule* (ID or path) to ``(capsule_id, capsule_path, manifest_path)``.

    Resolution order:

    1. *capsule* is an existing directory → use it (manifest optional);
    2. ``capsule_dir/<capsule>`` is an existing directory → use it;
    3. scan *capsule_dir* recursively for a ``redaction_manifest.json`` whose
       ``capsule_id`` equals *capsule*.

    Raises:
        CapsuleNotFoundError: If none of the above resolves.
    """
    candidate = Path(capsule)
    if candidate.is_dir():
        manifest_path = candidate / _MANIFEST_FILENAME
        return candidate.name, candidate, manifest_path if manifest_path.is_file() else None

    nested = capsule_dir / capsule
    if nested.is_dir():
        manifest_path = nested / _MANIFEST_FILENAME
        return capsule, nested, manifest_path if manifest_path.is_file() else None

    if capsule_dir.is_dir():
        for manifest_path in sorted(capsule_dir.rglob(_MANIFEST_FILENAME)):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("capsule_id") == capsule:
                return capsule, manifest_path.parent, manifest_path

    raise CapsuleNotFoundError(
        f"Capsule {capsule!r} not found: not a directory, and no "
        f"{_MANIFEST_FILENAME} under {capsule_dir} declares that capsule_id."
    )


def _active_subject_hmacs(
    dek_db_path: Path,
    pepper: bytes,
) -> dict[str, DEKSubjectRecord]:
    """Map subject HMAC → key-free DEK record for every live DEK in *dek_db_path*.

    Opens the existing store read-only in spirit (SQLite connection only;
    no rows are written). The caller must ensure the file already exists.
    """
    store = DEKStore(dek_db_path)
    try:
        return {
            _hmac_subject(pepper, record.subject_id): record
            for record in store.list_subjects()
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_pii_status(
    capsule: str,
    capsule_dir: Path | None = None,
    nova_home: Path | None = None,
    pepper: bytes | None = None,
) -> PIIStatusReport:
    """Build the read-only PII status report for one capsule.

    Args:
        capsule: Capsule ID or path to a capsule directory.
        capsule_dir: Directory scanned when *capsule* is a bare ID.
                     Defaults to ``$NOVAFABRIC_HOME/capsules/``.
        nova_home: NovaFabric home override.  Defaults to ``NOVAFABRIC_HOME``
                   env var or ``~/.novafabric``.
        pepper: HMAC pepper bytes.  Defaults to ``NOVA_PII_PEPPER`` env var
                if set; without a pepper, DEK correlation is ``unknown``.

    Returns:
        A :class:`PIIStatusReport`.

    Raises:
        CapsuleNotFoundError: If *capsule* cannot be resolved.
        ManifestInvalidError: If the capsule's manifest exists but is invalid.
    """
    if nova_home is None:
        nova_home = Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))
    if capsule_dir is None:
        capsule_dir = nova_home / "capsules"
    if pepper is None:
        raw = os.environ.get("NOVA_PII_PEPPER", "")
        pepper = raw.encode("utf-8") if raw else None

    capsule_id, capsule_path, manifest_path = _resolve_capsule(capsule, capsule_dir)

    manifest: RedactionManifest | None = None
    if manifest_path is not None:
        manifest = _load_manifest(manifest_path)
        if manifest.capsule_id:
            capsule_id = manifest.capsule_id

    fields = [
        PIIFieldStatus(
            field_path=entry.field_path,
            detection_rule_id=entry.detection_rule_id,
            subject_id_hmac=entry.subject_id_hmac,
            redacted_at_utc=entry.redacted_at_utc,
        )
        for entry in (manifest.entries if manifest is not None else [])
    ]

    # DEK correlation. Never create dek.db: check existence before opening.
    dek_db_path = nova_home / "dek.db"
    dek_store_present = dek_db_path.is_file()
    pepper_available = pepper is not None

    active_by_hmac: dict[str, DEKSubjectRecord] = {}
    if dek_store_present and pepper is not None:
        active_by_hmac = _active_subject_hmacs(dek_db_path, pepper)

    subjects: list[SubjectDEKStatus] = []
    for subject_hmac in sorted({f.subject_id_hmac for f in fields}):
        subject_fields = [f for f in fields if f.subject_id_hmac == subject_hmac]
        record = active_by_hmac.get(subject_hmac)
        state: Literal["active", "erased", "unknown"]
        if record is not None:
            state = "active"
        elif dek_store_present and not pepper_available:
            state = "unknown"
        else:
            # Either the DEK was crypto-shredded, or dek.db is absent entirely —
            # per ADR-0069 both mean the ciphertext is unrecoverable.
            state = "erased"
        subjects.append(
            SubjectDEKStatus(
                subject_id_hmac=subject_hmac,
                dek_state=state,
                dek_created_at=record.created_at if record is not None else None,
                tenant_id=record.tenant_id if record is not None else None,
                field_count=len(subject_fields),
                detection_rule_ids=sorted({f.detection_rule_id for f in subject_fields}),
                last_redacted_at_utc=max(f.redacted_at_utc for f in subject_fields),
            )
        )

    return PIIStatusReport(
        capsule_id=capsule_id,
        capsule_dir=str(capsule_path) if capsule_path is not None else None,
        manifest_present=manifest is not None,
        encrypted_field_count=len(fields),
        dek_store_path=str(dek_db_path),
        dek_store_present=dek_store_present,
        pepper_available=pepper_available,
        subjects=subjects,
        fields=fields,
        generated_at=datetime.now(timezone.utc),
    )
