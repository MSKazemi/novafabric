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
"""HIPAA Safe Harbor de-identification proof exporter.

Produces a structured technical evidence artifact documenting whether each of
the 18 HIPAA Safe Harbor identifier categories (45 CFR §164.514(b)(2)) is
absent, redacted, or not assessable from available capsule evidence.

DISCLAIMER: This artifact is technical evidence only.  It is NOT legal advice,
NOT a compliance certification, NOT HHS-approved, and does NOT guarantee that
any Safe Harbor legal standard is met.  Whether the output satisfies your
organisation's specific regulatory requirements must be determined by qualified
legal, compliance, and regulatory professionals.

Status: works today (v0.26.x) — reads capsule redaction evidence files only.
        Expert Determination workflow, NovaSeal signing, and inferred-PHI
        scanning are planned future extensions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The 18 Safe Harbor identifier categories from 45 CFR §164.514(b)(2).
SAFE_HARBOR_IDENTIFIERS: list[str] = [
    "names",
    "geographic_subdivisions",
    "dates_except_year",
    "telephone_numbers",
    "fax_numbers",
    "email_addresses",
    "social_security_numbers",
    "medical_record_numbers",
    "health_plan_beneficiary_numbers",
    "account_numbers",
    "certificate_license_numbers",
    "vehicle_identifiers",
    "device_identifiers",
    "urls",
    "ip_addresses",
    "biometric_identifiers",
    "full_face_photos",
    "other_unique_identifiers",
]

#: Human-readable labels for each identifier, ordered as in §164.514(b)(2).
IDENTIFIER_LABELS: dict[str, str] = {
    "names": "Names",
    "geographic_subdivisions": "Geographic subdivisions smaller than a state",
    "dates_except_year": "All elements of dates (except year) related to an individual",
    "telephone_numbers": "Telephone numbers",
    "fax_numbers": "Fax numbers",
    "email_addresses": "Electronic mail addresses",
    "social_security_numbers": "Social Security numbers",
    "medical_record_numbers": "Medical record numbers",
    "health_plan_beneficiary_numbers": "Health plan beneficiary numbers",
    "account_numbers": "Account numbers",
    "certificate_license_numbers": "Certificate and license numbers",
    "vehicle_identifiers": (
        "Vehicle identifiers and serial numbers, including license plate numbers"
    ),
    "device_identifiers": "Device identifiers and serial numbers",
    "urls": "Web Universal Resource Locators (URLs)",
    "ip_addresses": "Internet Protocol (IP) addresses",
    "biometric_identifiers": "Biometric identifiers, including finger and voice prints",
    "full_face_photos": "Full-face photographs and any comparable images",
    "other_unique_identifiers": "Any other unique identifying number, characteristic, or code",
}

#: Mapping from PII detector entity type strings to Safe Harbor identifier IDs.
#: Multiple entity-type aliases may map to the same Safe Harbor ID.
_ENTITY_TYPE_MAP: dict[str, str] = {
    # Names
    "PERSON": "names",
    "NAME": "names",
    "PER": "names",
    # Telephone numbers
    "PHONE": "telephone_numbers",
    "PHONE_NUMBER": "telephone_numbers",
    "PHONE_NUMBER_IN_FORMAT_XX": "telephone_numbers",
    # Email
    "EMAIL": "email_addresses",
    "EMAIL_ADDRESS": "email_addresses",
    # SSN
    "SSN": "social_security_numbers",
    "US_SSN": "social_security_numbers",
    "SOCIAL_SECURITY_NUMBER": "social_security_numbers",
    # Medical record numbers
    "MEDICAL_RECORD_NUMBER": "medical_record_numbers",
    "MRN": "medical_record_numbers",
    # IP address
    "IP_ADDRESS": "ip_addresses",
    "IP": "ip_addresses",
    # URL
    "URL": "urls",
    # Account numbers
    "CREDIT_CARD": "account_numbers",
    "BANK_ACCOUNT": "account_numbers",
    "ACCOUNT_NUMBER": "account_numbers",
    # Device identifiers
    "DEVICE_ID": "device_identifiers",
    "MAC_ADDRESS": "device_identifiers",
    # Vehicle
    "LICENSE_PLATE": "vehicle_identifiers",
    "VIN": "vehicle_identifiers",
    # Biometric
    "BIOMETRIC": "biometric_identifiers",
    "FINGERPRINT": "biometric_identifiers",
}

#: Legal disclaimer that MUST appear in every proof artifact.
LEGAL_DISCLAIMER: str = (
    "This artifact is a technical evidence record only. "
    "It is NOT legal advice, NOT a compliance certification, NOT HHS-approved, "
    "and does NOT guarantee HIPAA Safe Harbor sufficiency or any legal de-identification standard. "
    "NovaFabric cannot determine whether an operator has actual knowledge of re-identifiability "
    "from the remaining information (45 CFR §164.514(b)(1)(ii)). "
    "Image, biometric, and free-text inferred PHI assessment may require external expert review. "
    "Whether these capabilities satisfy your organisation's specific regulatory requirements "
    "must be determined by qualified legal, compliance, and regulatory professionals. "
    "Do not rely on this document as legal or compliance guidance."
)

#: Standard limitations list included in every proof artifact.
STANDARD_LIMITATIONS: list[str] = [
    "This artifact is technical evidence only and is not legal advice or compliance certification.",
    "NovaFabric cannot determine operator actual knowledge of re-identifiability "
    "(45 CFR §164.514(b)(1)(ii)).",
    "Image, biometric, and free-text inferred PHI assessment may require external expert review.",
    "Detection quality depends on the redaction evidence files present in the capsule directory "
    "and the configured scanners that produced them.",
    "NovaSeal cryptographic signing of this artifact is a planned future extension.",
]

#: Status vocabulary.
STATUS_NOT_PRESENT = "not_present"
STATUS_REDACTED = "redacted"
STATUS_PRESENT_UNREDACTED = "present_unredacted"
STATUS_NOT_ASSESSABLE = "not_assessable"

#: Identifiers that cannot be assessed from structured text redaction evidence alone.
_NOT_ASSESSABLE_DEFAULT: frozenset[str] = frozenset(
    {
        "full_face_photos",
        "biometric_identifiers",
    }
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class HIPAASafeHarborIdentifierAssessment(BaseModel):
    """Assessment of one Safe Harbor identifier category."""

    identifier: str = Field(..., description="Safe Harbor identifier key (snake_case).")
    label: str = Field(..., description="Human-readable label from §164.514(b)(2).")
    status: str = Field(
        ...,
        description=(
            "One of: 'not_present', 'redacted', 'present_unredacted', 'not_assessable'."
        ),
    )
    evidence_count: int = Field(
        default=0,
        description="Number of evidence items found in redaction files for this category.",
    )
    redaction_method: str | None = Field(
        default=None,
        description="Source of redaction evidence (e.g. 'redaction_manifest').",
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="References to specific evidence items (file:type pairs).",
    )


class HIPAASafeHarborProof(BaseModel):
    """HIPAA Safe Harbor de-identification proof artifact.

    This is a technical evidence record only.  See ``legal_disclaimer`` field.
    """

    schema_version: str = Field(default="1.0", description="Schema version of this proof format.")
    framework: str = Field(
        default="HIPAA Privacy Rule 45 CFR 164.514(b) Safe Harbor",
        description="Regulatory framework assessed.",
    )
    capsule_id: str = Field(..., description="Capsule identifier (directory name or run_id).")
    run_id: str = Field(..., description="Run identifier from capsule.yaml, or 'unknown'.")
    assessment_date: str = Field(
        ..., description="ISO 8601 UTC timestamp of assessment."
    )
    assessment_mode: str = Field(
        default="evidence_artifact_only",
        description=(
            "Assessment mode: 'evidence_artifact_only' means only structured "
            "redaction evidence is examined."
        ),
    )
    legal_disclaimer: str = Field(
        default=LEGAL_DISCLAIMER,
        description="Mandatory legal disclaimer. Must accompany any use of this artifact.",
    )
    identifiers: list[HIPAASafeHarborIdentifierAssessment] = Field(
        default_factory=list,
        description="Assessment for each of the 18 Safe Harbor identifier categories.",
    )
    proof_digest: str = Field(
        default="",
        description=(
            "SHA-256 digest of the canonical JSON of this proof "
            "(without the proof_digest field itself). Format: 'sha256:<hex>'."
        ),
    )
    source_files: list[str] = Field(
        default_factory=list,
        description="Capsule files examined to produce this assessment.",
    )
    expert_determination_ref: str | None = Field(
        default=None,
        description=(
            "Reference to an Expert Determination report (§164.514(c)), if any. "
            "Null when Expert Determination workflow has not been run."
        ),
    )
    limitations: list[str] = Field(
        default=STANDARD_LIMITATIONS,
        description="Known limitations of this assessment.",
    )


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------


class HIPAASafeHarborExporter:
    """Generates a HIPAA Safe Harbor de-identification proof for a capsule directory.

    The exporter reads available redaction evidence files from the capsule
    directory and maps PII entity types to Safe Harbor identifier categories.
    Categories for which no evidence exists are marked ``not_present``.
    Categories that cannot be assessed from structured evidence (e.g. images)
    are marked ``not_assessable``.

    No ML dependencies.  No cloud API calls.  Read-only access to capsule files.

    Args:
        not_assessable_ids: Set of Safe Harbor identifier IDs to mark as
            ``not_assessable`` by default (images, biometrics).  Override to
            expand or narrow the default set.
        assessment_datetime: Fixed datetime for use in tests.  If ``None``,
            the current UTC time is used.
    """

    def __init__(
        self,
        not_assessable_ids: frozenset[str] | None = None,
        assessment_datetime: datetime | None = None,
    ) -> None:
        self._not_assessable = (
            not_assessable_ids
            if not_assessable_ids is not None
            else _NOT_ASSESSABLE_DEFAULT
        )
        self._assessment_datetime = assessment_datetime

    def build_proof(self, capsule_dir: Path) -> HIPAASafeHarborProof:
        """Build a :class:`HIPAASafeHarborProof` from *capsule_dir*.

        Args:
            capsule_dir: Path to an unpacked capsule directory.

        Returns:
            A fully populated :class:`HIPAASafeHarborProof` including all 18
            identifier categories and a ``proof_digest``.
        """
        manifest = self._load_manifest(capsule_dir)
        run_id: str = manifest.get("run_id", "unknown") or "unknown"
        capsule_id: str = capsule_dir.name

        redaction_data, source_files = self._load_redaction_evidence(capsule_dir)
        if (capsule_dir / "capsule.yaml").exists():
            source_files = ["capsule.yaml"] + [f for f in source_files if f != "capsule.yaml"]

        # Map entity types -> Safe Harbor IDs, counting evidence per ID
        redacted_ids: dict[str, list[str]] = {}  # id -> list of evidence_refs
        for entry_type, source_label in redaction_data:
            safe_harbor_id = _ENTITY_TYPE_MAP.get(entry_type.upper(), "other_unique_identifiers")
            redacted_ids.setdefault(safe_harbor_id, [])
            redacted_ids[safe_harbor_id].append(source_label)

        assessment_date = (
            self._assessment_datetime or datetime.now(timezone.utc)
        ).isoformat()

        identifiers: list[HIPAASafeHarborIdentifierAssessment] = []
        for ident_id in SAFE_HARBOR_IDENTIFIERS:
            label = IDENTIFIER_LABELS[ident_id]
            if ident_id in self._not_assessable:
                assessment = HIPAASafeHarborIdentifierAssessment(
                    identifier=ident_id,
                    label=label,
                    status=STATUS_NOT_ASSESSABLE,
                    evidence_count=0,
                    redaction_method=None,
                    evidence_refs=[],
                )
            elif ident_id in redacted_ids:
                refs = redacted_ids[ident_id]
                assessment = HIPAASafeHarborIdentifierAssessment(
                    identifier=ident_id,
                    label=label,
                    status=STATUS_REDACTED,
                    evidence_count=len(refs),
                    redaction_method="redaction_manifest",
                    evidence_refs=refs,
                )
            else:
                assessment = HIPAASafeHarborIdentifierAssessment(
                    identifier=ident_id,
                    label=label,
                    status=STATUS_NOT_PRESENT,
                    evidence_count=0,
                    redaction_method=None,
                    evidence_refs=[],
                )
            identifiers.append(assessment)

        proof = HIPAASafeHarborProof(
            capsule_id=capsule_id,
            run_id=run_id,
            assessment_date=assessment_date,
            identifiers=identifiers,
            source_files=source_files,
            proof_digest="",  # computed below
        )

        proof.proof_digest = self._compute_digest(proof)
        return proof

    def export_json(self, proof: HIPAASafeHarborProof, output_path: Path) -> Path:
        """Write *proof* as JSON to *output_path*.

        Args:
            proof: The proof artifact to write.
            output_path: Destination path.

        Returns:
            The path written.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(proof.model_dump_json(indent=2), encoding="utf-8")
        return output_path

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_manifest(self, capsule_dir: Path) -> dict[str, Any]:
        """Load capsule.yaml; return empty dict if absent."""
        manifest_path = capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return {}
        try:
            import yaml  # already a core dependency

            with open(manifest_path) as fh:
                data = yaml.safe_load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_redaction_evidence(
        self, capsule_dir: Path
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Read all supported redaction evidence files from *capsule_dir*.

        Returns a tuple of:
        - List of (entity_type, source_label) tuples found across all files.
        - List of source file names that were found and read.
        """
        redaction_entries: list[tuple[str, str]] = []
        source_files_found: list[str] = []

        # Standard redaction manifest filenames (in preference order).
        candidate_filenames = [
            "redaction_manifest.json",
            "redaction-manifest.json",
            "redaction-proof.json",
        ]

        for filename in candidate_filenames:
            path = capsule_dir / filename
            if path.exists():
                entries = self._parse_redaction_file(path, filename)
                if entries:
                    redaction_entries.extend(entries)
                    source_files_found.append(filename)

        # Also check legal_hold/<capsule_dir_name>/redaction-manifest.json
        capsule_id = capsule_dir.name
        legal_hold_path = capsule_dir / "legal_hold" / capsule_id / "redaction-manifest.json"
        if legal_hold_path.exists() and "legal_hold" not in [
            s.split("/")[0] for s in source_files_found
        ]:
            lh_filename = f"legal_hold/{capsule_id}/redaction-manifest.json"
            entries = self._parse_redaction_file(legal_hold_path, lh_filename)
            if entries:
                redaction_entries.extend(entries)
                source_files_found.append(lh_filename)

        return redaction_entries, source_files_found

    def _parse_redaction_file(
        self, path: Path, source_label_prefix: str
    ) -> list[tuple[str, str]]:
        """Parse a redaction evidence JSON file.

        Supports two formats:
        - ``{"redacted_items": [{"pii_type": "EMAIL"}, ...]}`` (legacy flat format)
        - ``{"entries": [{"detection_rule_id": "EMAIL"}, ...]}`` (RedactionManifest format)

        Returns a list of (entity_type, source_label) tuples.
        """
        results: list[tuple[str, str]] = []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return results

        if not isinstance(data, dict):
            return results

        # Format 1: {"redacted_items": [...]}
        for item in data.get("redacted_items", []):
            pii_type = item.get("pii_type", "") if isinstance(item, dict) else ""
            if pii_type:
                results.append((pii_type.upper(), f"{source_label_prefix}:{pii_type}"))

        # Format 2: {"entries": [...]} (RedactionManifest schema)
        for entry in data.get("entries", []):
            rule_id = entry.get("detection_rule_id", "") if isinstance(entry, dict) else ""
            if rule_id:
                results.append((rule_id.upper(), f"{source_label_prefix}:{rule_id}"))

        return results

    @staticmethod
    def _compute_digest(proof: HIPAASafeHarborProof) -> str:
        """Compute SHA-256 over the canonical JSON of *proof* without ``proof_digest``.

        The digest is deterministic for fixed input.
        """
        payload = proof.model_dump(mode="json")
        payload.pop("proof_digest", None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest_hex = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"sha256:{digest_hex}"
