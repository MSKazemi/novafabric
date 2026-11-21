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

import pytest

from novafabric.compliance.export import euaiact_art12
from novafabric.compliance.export.euaiact_art12 import (
    ART12_RBI_REQUIREMENTS,
    ART12_REQUIREMENTS,
    Art12ConformanceReport,
    build_art12_report,
)
from novafabric.compliance.export.models import CompletenessSummaryEntry
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
        with pytest.raises(FileNotFoundError):
            build_art12_report(tmp_path, deployment_id="dep-1")


# ---------------------------------------------------------------------------
# Issue #63 — every label cites a provision that exists and says what it says
# (checked against the EUR-Lex text of Regulation (EU) 2024/1689).
# ---------------------------------------------------------------------------

#: The exact label of every row. A change to a label must change this table.
EXPECTED_LABELS: dict[str, str] = {
    "automatic_event_recording":
        "Art. 12(1) — the system technically allows the automatic recording of events "
        "(logs) over its lifetime",
    "period_of_use":
        "Art. 12(3)(a) — recording of the period of each use (start and end date and "
        "time); required for remote biometric identification systems (Annex III point "
        "1(a)), reported for every run",
    "traceability":
        "Art. 12(2), chapeau — logging ensures a level of traceability of the system's "
        "functioning appropriate to its intended purpose",
    "input_output_records":
        "NovaFabric evidence, not an Act duty of its own — recorded model inputs and "
        "outputs, supporting the Art. 12(2)(a)-(c) purposes (for remote biometric "
        "identification see also Art. 12(3)(c))",
    "tamper_evidence":
        "Standards item, beyond the Act's text — log integrity protection "
        "(prEN 18229-1; ISO/IEC 24970). The AI Act sets no log tamper-protection duty",
    "retention":
        "Art. 19(1) (providers) / Art. 26(6) (deployers) — automatically generated logs "
        "under the party's control kept for a period appropriate to the intended "
        "purpose, at least six months unless Union or national law provides otherwise",
    "risk_situation_events":
        "Art. 12(2)(a) — logging enables recording of events relevant for identifying "
        "situations that may present a risk within the meaning of Art. 79(1) or lead "
        "to a substantial modification",
    "post_market_monitoring_events":
        "Art. 12(2)(b) — logging enables recording of events relevant for facilitating "
        "post-market monitoring (Art. 72)",
    "deployer_monitoring_events":
        "Art. 12(2)(c) — logging enables recording of events relevant for monitoring "
        "the system's operation (Art. 26(5))",
    "log_instructions_for_use":
        "Art. 13(3)(f) — the instructions for use describe, where relevant, the "
        "mechanisms that let deployers properly collect, store and interpret the logs",
    "authority_log_access":
        "Art. 21(2) — on a reasoned request, the provider gives the competent authority "
        "access to the automatically generated logs under its control",
}

EXPECTED_RBI_LABELS: dict[str, str] = {
    "rbi_reference_database":
        "Art. 12(3)(b) — the reference database against which input data has been "
        "checked (remote biometric identification only)",
    "rbi_matched_input_data":
        "Art. 12(3)(c) — the input data for which the search has led to a match "
        "(remote biometric identification only)",
    "rbi_verifying_persons":
        "Art. 12(3)(d) — identification of the natural persons involved in verifying "
        "the results, as referred to in Art. 14(5) (remote biometric identification "
        "only)",
}

#: The six keys the report carried before issue #63, in their original order.
ORIGINAL_KEYS = (
    "automatic_event_recording", "period_of_use", "traceability",
    "input_output_records", "tamper_evidence", "retention",
)

PURPOSE_KEYS = (
    "risk_situation_events", "post_market_monitoring_events", "deployer_monitoring_events",
)


def _row(report: Art12ConformanceReport, key: str) -> CompletenessSummaryEntry:
    return next(e for e in report.completeness_summary if e.field_name == key)


class TestArt12Labels:
    def test_every_label_is_pinned(self) -> None:
        assert dict(ART12_REQUIREMENTS) == EXPECTED_LABELS

    def test_every_rbi_label_is_pinned(self) -> None:
        assert dict(ART12_RBI_REQUIREMENTS) == EXPECTED_RBI_LABELS

    def test_the_original_keys_keep_their_positions(self) -> None:
        # Additive change: no key removed, the first six rows unchanged in order.
        assert tuple(k for k, _ in ART12_REQUIREMENTS[:6]) == ORIGINAL_KEYS

    def test_no_label_repeats_a_wrong_citation(self) -> None:
        labels = dict(ART12_REQUIREMENTS)
        assert "lifetime" in labels["automatic_event_recording"]
        assert not labels["period_of_use"].startswith("Art. 12(2)")
        assert "Art. 12(3)" not in labels["retention"]
        assert "Art. 19(1)" in labels["retention"] and "Art. 26(6)" in labels["retention"]
        # The Act has no log tamper-protection duty: a standards item, not an Art. cite.
        assert not labels["tamper_evidence"].startswith("Art.")
        assert "beyond the Act's text" in labels["tamper_evidence"]
        assert not labels["input_output_records"].startswith("Art.")

    def test_tamper_and_io_rows_are_not_labelled_act_duties(
        self, minimal_capsule_dir: Path
    ) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        basis = {r.field_name: r.basis for r in report.requirements}
        assert basis.pop("tamper_evidence") == "standards"
        assert basis.pop("input_output_records") == "novafabric_evidence"
        assert set(basis.values()) == {"ai_act"}

    def test_requirements_mirror_the_summary_rows(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        assert [r.field_name for r in report.requirements] == [
            e.field_name for e in report.completeness_summary
        ]
        assert {r.field_name: r.label for r in report.requirements} == EXPECTED_LABELS


class TestArt12NewRows:
    def test_rbi_rows_absent_by_default(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        keys = [e.field_name for e in report.completeness_summary]
        assert keys == list(EXPECTED_LABELS)
        assert report.remote_biometric_identification is False

    def test_rbi_rows_present_when_declared(self, minimal_capsule_dir: Path) -> None:
        report = build_art12_report(
            minimal_capsule_dir, deployment_id="dep-1", remote_biometric_identification=True
        )
        keys = [e.field_name for e in report.completeness_summary]
        assert keys == [*EXPECTED_LABELS, *EXPECTED_RBI_LABELS]
        assert [r.field_name for r in report.requirements] == keys
        assert report.remote_biometric_identification is True
        validate_marked(report.completeness_summary)
        # NovaFabric does not record the reference database or the verifying persons.
        assert _row(report, "rbi_reference_database").status == "missing"
        assert _row(report, "rbi_verifying_persons").status == "missing"
        matched = _row(report, "rbi_matched_input_data")
        assert matched.status == "partial"
        assert matched.evidence_source is EvidenceSource.capsule_verified

    def test_rbi_matched_input_missing_without_inputs(self, tmp_path: Path) -> None:
        # No model-calls stream and no model_calls_ref: no input records at all.
        (tmp_path / "capsule.yaml").write_text("run_id: r1\ncreated_at: '2024-01-01'\n")
        report = build_art12_report(
            tmp_path, deployment_id="dep-1", remote_biometric_identification=True
        )
        matched = _row(report, "rbi_matched_input_data")
        assert matched.status == "missing"
        assert matched.evidence_source is EvidenceSource.unverifiable

    def test_art12_2_purpose_rows_are_partial_with_events(
        self, minimal_capsule_dir: Path
    ) -> None:
        # The events exist; whether they are the relevant ones is the provider's call.
        report = build_art12_report(minimal_capsule_dir, deployment_id="dep-1")
        for key in PURPOSE_KEYS:
            row = _row(report, key)
            assert row.status == "partial", key
            assert row.evidence_source is EvidenceSource.capsule_verified
            assert row.evidence_ref is not None

    def test_art12_2_purpose_rows_missing_without_events(self, tmp_path: Path) -> None:
        (tmp_path / "capsule.yaml").write_text("run_id: r1\ncreated_at: '2024-01-01'\n")
        report = build_art12_report(tmp_path, deployment_id="dep-1")
        for key in (*PURPOSE_KEYS, "authority_log_access"):
            row = _row(report, key)
            assert row.status == "missing", key
            assert row.evidence_source is EvidenceSource.unverifiable

    def test_instructions_for_use_is_operator_asserted(self, minimal_capsule_dir: Path) -> None:
        missing = _row(
            build_art12_report(minimal_capsule_dir, deployment_id="dep-1"),
            "log_instructions_for_use",
        )
        assert missing.status == "missing"
        assert missing.evidence_source is EvidenceSource.operator_asserted
        given = _row(
            build_art12_report(
                minimal_capsule_dir, deployment_id="dep-1",
                log_instructions_ref="IFU v2 section 7",
            ),
            "log_instructions_for_use",
        )
        assert given.status == "complete"
        assert given.evidence_source is EvidenceSource.operator_asserted
        assert given.evidence_ref is None
        assert "IFU v2 section 7" in given.reason

    def test_authority_access_names_nova_verify_only_when_sealed(
        self, minimal_capsule_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unsealed = _row(
            build_art12_report(minimal_capsule_dir, deployment_id="dep-1"),
            "authority_log_access",
        )
        assert unsealed.status == "partial"
        assert "cannot check its integrity" in unsealed.reason
        monkeypatch.setattr(euaiact_art12, "seal_for_log_claims", lambda _d: "unbound")
        unbound = _row(
            build_art12_report(minimal_capsule_dir, deployment_id="dep-1"),
            "authority_log_access",
        )
        assert unbound.status == "partial"
        assert "does not bind the evidence streams" in unbound.reason
        assert "check it offline" not in unbound.reason
        monkeypatch.setattr(euaiact_art12, "seal_for_log_claims", lambda _d: "bound")
        sealed = _row(
            build_art12_report(minimal_capsule_dir, deployment_id="dep-1"),
            "authority_log_access",
        )
        assert sealed.status == "partial"
        assert "check it offline with `nova verify`" in sealed.reason

    def test_json_keeps_every_previous_top_level_key(self, minimal_capsule_dir: Path) -> None:
        dumped = build_art12_report(minimal_capsule_dir, deployment_id="dep-1").model_dump(
            mode="json"
        )
        assert {"standard", "deployment_id", "generated_at", "completeness_summary",
                "disclaimer"} <= set(dumped)
        assert "requirements" in dumped and "remote_biometric_identification" in dumped
        assert "conforming" not in dumped and "verdict" not in dumped
