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
"""ADR-0107 §NF-093 — GPAI Art. 53 Model Documentation Form.

Art. 53(1) documentation kept as a **sealed, hash-chained revision history**: each
revision is canonically hashed and links to its predecessor (a tamper-evident
material-change record), each carries a 10-year ``retention_until``, and any two
revisions are field-level **diffable**.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from novafabric.compliance.export.gpai53 import (
    GPAI_ART53_RETENTION_YEARS,
    append_revision,
    build_gpai53_form,
    diff_revisions,
    verify_history,
)

_T0 = datetime(2026, 7, 25, tzinfo=timezone.utc)
_T1 = datetime(2026, 9, 1, tzinfo=timezone.utc)

_FIELDS = {
    "general_description": "A general-purpose language model.",
    "training_data_summary": "Public web corpus, filtered.",
    "energy_consumption": "1.2 GWh (training).",
}


class TestBuildForm:
    def test_first_revision_is_sealed_and_unlinked(self) -> None:
        form = build_gpai53_form("gpai-x", initial_fields=_FIELDS, created_at=_T0)
        assert len(form.revisions) == 1
        rev = form.revisions[0]
        assert rev.revision == 1
        assert rev.prev_digest is None
        assert rev.content_digest.startswith("sha256:")

    def test_retention_is_ten_years(self) -> None:
        form = build_gpai53_form("gpai-x", initial_fields=_FIELDS, created_at=_T0)
        rev = form.revisions[0]
        assert GPAI_ART53_RETENTION_YEARS == 10
        assert rev.retention_until.startswith("2036-07-25")


class TestMaterialChangeHistory:
    def test_append_seals_and_chains_a_revision(self) -> None:
        form = build_gpai53_form("gpai-x", initial_fields=_FIELDS, created_at=_T0)
        changed = {**_FIELDS, "energy_consumption": "1.5 GWh (training, revised)."}
        form = append_revision(form, fields=changed, created_at=_T1)
        assert len(form.revisions) == 2
        r1, r2 = form.revisions
        assert r2.revision == 2
        # The chain links the new revision to its predecessor's digest.
        assert r2.prev_digest == r1.content_digest
        assert r2.content_digest != r1.content_digest

    def test_verify_history_accepts_an_untampered_chain(self) -> None:
        form = build_gpai53_form("gpai-x", initial_fields=_FIELDS, created_at=_T0)
        form = append_revision(form, fields={**_FIELDS, "x": "y"}, created_at=_T1)
        assert verify_history(form) is True

    def test_verify_history_rejects_a_tampered_revision(self) -> None:
        form = build_gpai53_form("gpai-x", initial_fields=_FIELDS, created_at=_T0)
        form = append_revision(form, fields={**_FIELDS, "x": "y"}, created_at=_T1)
        # Tamper with a sealed revision's content without recomputing its digest.
        form.revisions[0].fields["general_description"] = "SILENTLY ALTERED"
        assert verify_history(form) is False

    def test_verify_history_rejects_a_broken_link(self) -> None:
        form = build_gpai53_form("gpai-x", initial_fields=_FIELDS, created_at=_T0)
        form = append_revision(form, fields={**_FIELDS, "x": "y"}, created_at=_T1)
        form.revisions[1].prev_digest = "sha256:" + "0" * 64
        assert verify_history(form) is False


class TestDiff:
    def test_diff_reports_added_removed_modified(self) -> None:
        form = build_gpai53_form("gpai-x", initial_fields=_FIELDS, created_at=_T0)
        new_fields = {
            "general_description": "A general-purpose language model.",  # unchanged
            "training_data_summary": "Public web corpus, filtered and deduplicated.",  # modified
            # energy_consumption removed
            "systemic_risk_assessment": "No systemic risk identified.",  # added
        }
        form = append_revision(form, fields=new_fields, created_at=_T1)
        changes = {c.field: c.change for c in diff_revisions(form.revisions[0], form.revisions[1])}
        assert changes["training_data_summary"] == "modified"
        assert changes["energy_consumption"] == "removed"
        assert changes["systemic_risk_assessment"] == "added"
        assert "general_description" not in changes  # unchanged fields are not reported


def test_build_requires_fields() -> None:
    with pytest.raises(ValueError, match="fields"):
        build_gpai53_form("gpai-x", initial_fields={}, created_at=_T0)
