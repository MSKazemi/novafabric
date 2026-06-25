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
"""Incident entity + lifecycle tests (ADR-0088)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from novafabric.compliance.incident.models import (
    Incident,
    IncidentSeverity,
    IncidentStatus,
    IncidentTransitionError,
)

OCCURRED = datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc)
AWARE = datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc)


def _incident(**overrides: object) -> Incident:
    base: dict[str, object] = {
        "title": "Tool misuse",
        "classification": "unauthorized_tool_use",
        "severity": IncidentSeverity.HIGH,
        "occurred_at": OCCURRED,
    }
    base.update(overrides)
    return Incident(**base)  # type: ignore[arg-type]


class TestIncidentModel:
    def test_defaults(self) -> None:
        inc = _incident()
        assert inc.id.startswith("inc-")
        assert inc.status is IncidentStatus.OPEN
        assert inc.clock_anchor == OCCURRED

    def test_aware_at_is_clock_anchor_when_set(self) -> None:
        inc = _incident(aware_at=AWARE)
        assert inc.clock_anchor == AWARE

    def test_aware_at_before_occurred_at_rejected(self) -> None:
        with pytest.raises(ValidationError, match="aware_at cannot be earlier"):
            _incident(
                occurred_at=AWARE,
                aware_at=OCCURRED,
            )

    def test_naive_timestamp_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timezone-aware"):
            _incident(occurred_at=datetime(2026, 6, 10, 8, 0))


class TestLifecycle:
    def test_forward_transitions_allowed_and_audited(self) -> None:
        inc = _incident()
        reported = inc.transitioned(IncidentStatus.REPORTED, at=AWARE)
        closed = reported.transitioned(IncidentStatus.CLOSED, at=AWARE)
        assert closed.status is IncidentStatus.CLOSED
        assert [t.to_value for t in closed.history] == ["reported", "closed"]

    def test_open_to_closed_skip_allowed(self) -> None:
        inc = _incident()
        assert (
            inc.transitioned(IncidentStatus.CLOSED, at=AWARE).status
            is IncidentStatus.CLOSED
        )

    def test_backward_transition_rejected(self) -> None:
        reported = _incident().transitioned(IncidentStatus.REPORTED, at=AWARE)
        with pytest.raises(IncidentTransitionError, match="forward-only"):
            reported.transitioned(IncidentStatus.OPEN, at=AWARE)

    def test_noop_transition_rejected(self) -> None:
        inc = _incident()
        with pytest.raises(IncidentTransitionError):
            inc.transitioned(IncidentStatus.OPEN, at=AWARE)

    def test_attestation_attach_is_audited(self) -> None:
        inc = _incident().with_attestation("attest-001", at=AWARE)
        assert inc.attestation_ref == "attest-001"
        assert inc.history[-1].kind == "attestation"
        assert inc.history[-1].to_value == "attest-001"
