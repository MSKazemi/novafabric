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
"""Tests for the ADR-0197 evidence_source provenance marker primitive."""

from __future__ import annotations

import pytest

from novafabric.compliance.export.models import CompletenessSummaryEntry
from novafabric.compliance.export.provenance import (
    EvidenceSource,
    EvidenceSourceRef,
    MissingReperformableRefError,
    UnmarkedFieldGroupError,
    mark,
    validate_marked,
)


def _ref() -> EvidenceSourceRef:
    return EvidenceSourceRef(
        capsule_id="cap-123",
        content_digest="sha256:" + "a" * 64,
        verified_at="2026-07-24T00:00:00Z",
    )


class TestEvidenceSourceEnum:
    def test_three_values_exist(self) -> None:
        assert EvidenceSource.operator_asserted == "operator_asserted"
        assert EvidenceSource.capsule_verified == "capsule_verified"
        assert EvidenceSource.unverifiable == "unverifiable"

    def test_no_fourth_value(self) -> None:
        assert {e.value for e in EvidenceSource} == {
            "operator_asserted",
            "capsule_verified",
            "unverifiable",
        }


class TestMark:
    def test_operator_asserted_needs_no_ref(self) -> None:
        source, ref = mark(EvidenceSource.operator_asserted)
        assert source is EvidenceSource.operator_asserted
        assert ref is None

    def test_unverifiable_needs_no_ref(self) -> None:
        source, ref = mark(EvidenceSource.unverifiable)
        assert source is EvidenceSource.unverifiable
        assert ref is None

    def test_capsule_verified_requires_ref(self) -> None:
        # I-3: a verification you cannot re-perform is not a verification.
        with pytest.raises(MissingReperformableRefError):
            mark(EvidenceSource.capsule_verified)

    def test_capsule_verified_with_ref_ok(self) -> None:
        source, ref = mark(EvidenceSource.capsule_verified, ref=_ref())
        assert source is EvidenceSource.capsule_verified
        assert ref is not None
        assert ref.capsule_id == "cap-123"

    def test_non_verified_rejects_stray_ref(self) -> None:
        # An operator assertion carrying a capsule ref would misrepresent provenance.
        with pytest.raises(ValueError):
            mark(EvidenceSource.operator_asserted, ref=_ref())


class TestValidateMarked:
    def test_all_marked_passes(self) -> None:
        entries = [
            CompletenessSummaryEntry(
                field_name="f1",
                status="complete",
                reason="ok",
                evidence_source=EvidenceSource.operator_asserted,
            ),
            CompletenessSummaryEntry(
                field_name="f2",
                status="complete",
                reason="ok",
                evidence_source=EvidenceSource.capsule_verified,
                evidence_ref=_ref(),
            ),
        ]
        validate_marked(entries)  # must not raise

    def test_unmarked_entry_raises_when_required(self) -> None:
        entries = [
            CompletenessSummaryEntry(field_name="f1", status="complete", reason="ok"),
        ]
        with pytest.raises(UnmarkedFieldGroupError):
            validate_marked(entries, require=True)

    def test_unmarked_allowed_when_not_required(self) -> None:
        entries = [
            CompletenessSummaryEntry(field_name="f1", status="complete", reason="ok"),
        ]
        validate_marked(entries, require=False)  # must not raise

    def test_capsule_verified_without_ref_raises(self) -> None:
        # I-3 enforced at document level too, not only via mark().
        entries = [
            CompletenessSummaryEntry(
                field_name="f1",
                status="complete",
                reason="ok",
                evidence_source=EvidenceSource.capsule_verified,
            ),
        ]
        with pytest.raises(MissingReperformableRefError):
            validate_marked(entries)

    def test_mixed_document_representable(self) -> None:
        # D3: a document that verifies some fields and asserts others is valid.
        entries = [
            CompletenessSummaryEntry(
                field_name="verified",
                status="complete",
                reason="from capsule",
                evidence_source=EvidenceSource.capsule_verified,
                evidence_ref=_ref(),
            ),
            CompletenessSummaryEntry(
                field_name="asserted",
                status="complete",
                reason="operator input",
                evidence_source=EvidenceSource.operator_asserted,
            ),
            CompletenessSummaryEntry(
                field_name="gated",
                status="missing",
                reason="requires cap-006",
                evidence_source=EvidenceSource.unverifiable,
            ),
        ]
        validate_marked(entries)  # must not raise


class TestBackwardCompatibility:
    def test_entry_deserializes_without_marker(self) -> None:
        # An envelope produced before ADR-0197 still validates (marker optional).
        entry = CompletenessSummaryEntry.model_validate(
            {"field_name": "f1", "status": "complete", "reason": "legacy"}
        )
        assert entry.evidence_source is None
        assert entry.evidence_ref is None
