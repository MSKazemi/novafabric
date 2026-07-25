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
"""ADR-0107 NF-090 — EU AI Act Art. 12 automatic-logging conformance exporter.

A render-from-sealed-facts exporter: it maps a run capsule's captured evidence
(event streams, use-period timestamps, trace, seal, retention) to the Art. 12
record-keeping requirements and marks each `complete` / `partial` / `missing` with
an ADR-0197 `evidence_source`. It renders facts — it does not certify conformity.
"""

from __future__ import annotations

from pathlib import Path

from novafabric.compliance.export.euaiact_art12 import (
    ART12_REQUIREMENTS,
    build_art12_report,
)
from novafabric.compliance.export.provenance import EvidenceSource, validate_marked


class TestArt12Exporter:
    def test_all_requirements_present(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        keys = {e.field_name for e in report.completeness_summary}
        assert keys == {r[0] for r in ART12_REQUIREMENTS}

    def test_every_entry_is_provenance_marked(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        for e in report.completeness_summary:
            assert e.evidence_source is not None
        validate_marked(report.completeness_summary)

    def test_event_recording_is_capsule_verified(self, minimal_capsule_dir: Path) -> None:
        # The capsule carries model-calls/tool-calls/trace streams → automatic
        # event recording is capsule_verified with a re-performable ref.
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        rec = next(e for e in report.completeness_summary if e.field_name == "automatic_event_recording")
        assert rec.evidence_source is EvidenceSource.capsule_verified
        assert rec.evidence_ref is not None

    def test_period_of_use_from_timestamps(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        per = next(e for e in report.completeness_summary if e.field_name == "period_of_use")
        assert per.status == "complete"

    def test_retention_without_operator_input_is_missing(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        ret = next(e for e in report.completeness_summary if e.field_name == "retention")
        assert ret.status == "missing"

    def test_retention_operator_asserted_when_provided(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1", retention_days=180)
        ret = next(e for e in report.completeness_summary if e.field_name == "retention")
        assert ret.status == "complete"
        assert ret.evidence_source is EvidenceSource.operator_asserted

    def test_tamper_evidence_missing_without_seal(self, minimal_capsule_dir: Path) -> None:
        # The minimal capsule carries no seal envelope → tamper-evidence is unverifiable.
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        tamper = next(e for e in report.completeness_summary if e.field_name == "tamper_evidence")
        assert tamper.evidence_source is EvidenceSource.unverifiable

    def test_report_metadata(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        assert report.deployment_id == "dep-1"
        assert "Art" in report.standard and "12" in report.standard
        assert report.generated_at

    def test_no_capsule_yaml_raises(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(FileNotFoundError):
            build_art12_report(tmp_path, deployment_id="dep-1")
