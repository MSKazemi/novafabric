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
"""Redaction manifest models (cap-001)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RedactionManifestEntry(BaseModel):
    """A single redacted field record within a :class:`RedactionManifest`.

    OQ-01 (GDPR Art.17) resolved by ADR-0069 — cap-001 is active.
    """

    field_path: str = Field(
        ...,
        description=(
            "Dot-notation path to the redacted field within the capsule payload, "
            "e.g. 'model_calls[0].messages[1].content'"
        ),
    )
    detection_rule_id: str = Field(
        ...,
        description="Rule identifier that triggered the redaction (e.g. 'EMAIL', 'PHONE')",
    )
    legal_basis: str = Field(
        ...,
        description=(
            "Legal basis for erasure / redaction (GDPR Art.17 erasure basis, "
            "or operator-declared compliance obligation)"
        ),
    )
    subject_id_hmac: str = Field(
        ...,
        description=(
            "HMAC-SHA256 of (subject_id + pepper) encoded as 'sha256:<hex>'. "
            "The pepper is read-only from NOVA_PII_PEPPER env var and is NEVER "
            "stored in any capsule, log, or config file."
        ),
    )
    redacted_at_utc: str = Field(
        ...,
        description="ISO 8601 UTC timestamp when the redaction was applied",
    )

    model_config = {"frozen": True}


class RedactionManifest(BaseModel):
    """Complete redaction manifest for a single capsule.

    Status: ACTIVE (OQ-01 resolved by ADR-0069, 2026-05-27).
    cap-001 graduated from LEGAL-HOLD DRAFT.
    """

    capsule_id: str = Field(..., description="ULID of the capsule this manifest covers")
    entries: list[RedactionManifestEntry] = Field(
        default_factory=list,
        description="All redacted fields in this capsule",
    )
    legal_hold_mode: bool = Field(
        default=False,
        description=(
            "False now that OQ-01 (GDPR Art.17) is resolved by ADR-0069.  "
            "Manifests are available for sealing into capsules.  "
            "Field retained for schema compatibility with pre-v0.25 capsules."
        ),
    )

    def append(self, entry: RedactionManifestEntry) -> "RedactionManifest":
        """Return a new manifest with *entry* appended (immutable pattern)."""
        return RedactionManifest(
            capsule_id=self.capsule_id,
            entries=[*self.entries, entry],
            legal_hold_mode=self.legal_hold_mode,
        )
