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
"""IncidentStore + exporter tests (ADR-0088)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from novafabric.compliance.export.models import NIS2IncidentReport
from novafabric.compliance.incident.aim_export import (
    build_aim_report,
    build_nis2_report_from_incident,
)
from novafabric.compliance.incident.models import (
    Incident,
    IncidentNotFoundError,
    IncidentSeverity,
    IncidentStatus,
)
from novafabric.compliance.incident.store import IncidentStore, incidents_db_path

OCCURRED = datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)
AWARE = datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc)


def test_aim_completeness_marked_operator_asserted() -> None:
    # ADR-0197 I-1: AIM report projects operator-authored incident data.
    report = build_aim_report(_incident(id="inc-aim"))
    entries = report["completeness_summary"]
    assert entries
    for entry in entries:
        assert entry["evidence_source"] == "operator_asserted"


def test_nis2_from_incident_marked() -> None:
    from novafabric.compliance.export.provenance import (
        EvidenceSource,
        validate_marked,
    )

    report = build_nis2_report_from_incident(_incident(id="inc-nis2"))
    assert report.completeness_summary
    for entry in report.completeness_summary:
        assert entry.evidence_source is EvidenceSource.operator_asserted
    validate_marked(report.completeness_summary)


def _incident(**overrides: object) -> Incident:
    base: dict[str, object] = {
        "title": "Tool misuse",
        "classification": "unauthorized_tool_use",
        "severity": IncidentSeverity.HIGH,
        "occurred_at": OCCURRED,
        "aware_at": AWARE,
        "run_ids": ["run-abc"],
    }
    base.update(overrides)
    return Incident(**base)  # type: ignore[arg-type]


@pytest.fixture()
def store(tmp_path: Path) -> IncidentStore:
    with IncidentStore(db_path=tmp_path / "incidents.db") as s:
        yield s


class TestIncidentStore:
    def test_create_get_round_trip(self, store: IncidentStore) -> None:
        inc = store.create(_incident())
        loaded = store.get(inc.id)
        assert loaded == inc

    def test_duplicate_id_rejected(self, store: IncidentStore) -> None:
        inc = store.create(_incident())
        with pytest.raises(ValueError, match="already exists"):
            store.create(inc)

    def test_get_missing_raises_named_error(self, store: IncidentStore) -> None:
        with pytest.raises(IncidentNotFoundError):
            store.get("inc-doesnotexist")

    def test_list_orders_newest_first(self, store: IncidentStore) -> None:
        older = store.create(_incident(aware_at=None))
        newer = store.create(
            _incident(
                occurred_at=AWARE,
                aware_at=None,
                title="later",
            )
        )
        assert [i.id for i in store.list_all()] == [newer.id, older.id]

    def test_transition_persists_and_enforces_forward_only(
        self, store: IncidentStore
    ) -> None:
        inc = store.create(_incident())
        store.transition(inc.id, IncidentStatus.REPORTED, at=AWARE)
        assert store.get(inc.id).status is IncidentStatus.REPORTED
        from novafabric.compliance.incident.models import IncidentTransitionError

        with pytest.raises(IncidentTransitionError):
            store.transition(inc.id, IncidentStatus.OPEN, at=AWARE)

    def test_attach_attestation_round_trip(self, store: IncidentStore) -> None:
        inc = store.create(_incident())
        store.attach_attestation(inc.id, "attest-001", at=AWARE)
        loaded = store.get(inc.id)
        assert loaded.attestation_ref == "attest-001"
        assert loaded.history[-1].kind == "attestation"

    def test_default_path_under_novafabric_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
        monkeypatch.delenv("NOVAFABRIC_INCIDENTS_DB_PATH", raising=False)
        assert incidents_db_path() == tmp_path / "incidents.db"


class TestExporters:
    def test_aim_report_is_valid_json_with_required_keys(self) -> None:
        report = build_aim_report(_incident(), now=AWARE)
        payload = json.loads(json.dumps(report, default=str))
        assert payload["report_type"] == "oecd-aim"
        assert payload["incident_id"].startswith("inc-")
        assert payload["linked_run_capsules"] == ["run-abc"]
        assert payload["art73_deadlines"], "deadline table must be embedded"
        assert "not legal advice" in payload["disclaimer"]

    def test_aim_report_marks_missing_fields_with_reason(self) -> None:
        report = build_aim_report(_incident(), now=AWARE)
        missing = {e["field_name"]: e for e in report["completeness_summary"]}
        assert missing["harmed_parties"]["status"] == "missing"
        assert missing["harmed_parties"]["reason"]

    def test_nis2_from_incident_returns_valid_model(self) -> None:
        report = build_nis2_report_from_incident(_incident(), now=AWARE)
        assert isinstance(report, NIS2IncidentReport)
        assert report.first_detection_timestamp == AWARE.isoformat()
        assert report.preliminary_severity_indicator == "high"
        names = {e.field_name for e in report.completeness_summary}
        assert "lessons_learned" in names
