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

"""Continuous-assurance attestation — ADR-0147 D7 / NF-159.

Records that a scheduled assurance run executed: which baselines it checked
(NF-160 pins), which detectors it ran, how many alarms fired, and when the next
run is due.

**The part worth reading before changing anything here.** ADR-0147 requires that a
*missed* run be detectable. A missed run writes nothing — there is no record of it
to inspect. Absence only becomes evidence because every successful run states when
the next one is **due**, so a miss is detected against the *previous* attestation's
promise rather than against the missing one. The artifact that proves the failure
is the last success.

That makes ``next_due`` the mechanism, not a convenience field, which is why it is
**derived** from ``ran_at`` plus the cadence rather than accepted from the caller.
A caller-supplied ``next_due`` could promise a date that never arrives, and the
overdue check would then never fire — the requirement would be unmeetable while
appearing to be implemented.

Sealing reuses the existing in-toto statement through
``envelopes.intoto.capsule_statement(extra_predicate=...)``. No second attestation
format is introduced (ADR-0034's two-format rule).

Fail-open: no attestation ⇒ no facet ⇒ the capsule is byte-identical.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "assurance_attestation"

#: Predicate key under which the attestation seals into the in-toto statement.
PREDICATE_KEY = "assuranceAttestation"


class AttestationError(ValueError):
    """An assurance attestation could not be built or interpreted."""


def _parse_rfc3339(value: str, *, field: str) -> datetime:
    """Parse an RFC 3339 timestamp into an aware UTC datetime.

    Timestamps are validated rather than trusted: the overdue check is arithmetic
    on these values, so an unparseable or naive one would silently produce a
    verdict computed from nonsense.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AttestationError(
            f"{field} must be an RFC 3339 timestamp, got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise AttestationError(
            f"{field} must carry a UTC offset; a naive timestamp cannot be compared "
            "across hosts"
        )
    return parsed.astimezone(timezone.utc)


def _to_rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AssuranceAttestation(BaseModel):
    """Proof that one scheduled assurance run executed (NF-159)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = SCHEMA_VERSION
    schedule_id: str
    ran_at: str
    #: NF-160 ``baseline_id``s this run measured against. Empty is legitimate and
    #: visible — a run that checked nothing is a fact worth recording, not an error.
    baselines_checked: list[str] = Field(default_factory=list)
    detectors_run: list[str] = Field(default_factory=list)
    alarms_fired: int = 0
    #: Derived, never supplied. See the module docstring.
    next_due: str
    cadence_seconds: int
    sealed_into: str = "in-toto"

    @field_validator("schedule_id")
    @classmethod
    def _check_schedule_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("schedule_id must not be empty")
        return v

    @field_validator("alarms_fired")
    @classmethod
    def _check_alarms(cls, v: int) -> int:
        if v < 0:
            raise ValueError("alarms_fired cannot be negative")
        return v

    @field_validator("cadence_seconds")
    @classmethod
    def _check_cadence(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(
                "cadence_seconds must be positive; a non-positive cadence makes "
                "next_due meaningless and the overdue check unmeetable"
            )
        return v


def record_run(
    schedule_id: str,
    *,
    ran_at: str,
    cadence_seconds: int,
    baselines_checked: list[str] | None = None,
    detectors_run: list[str] | None = None,
    alarms_fired: int = 0,
) -> AssuranceAttestation:
    """Record that a scheduled assurance run executed.

    ``next_due`` is computed here — ``ran_at`` + cadence — and is deliberately not
    a parameter.
    """
    if cadence_seconds <= 0:
        raise AttestationError("cadence_seconds must be positive")
    moment = _parse_rfc3339(ran_at, field="ran_at")
    due = moment + timedelta(seconds=cadence_seconds)
    try:
        return AssuranceAttestation(
            schedule_id=schedule_id,
            ran_at=_to_rfc3339(moment),
            baselines_checked=list(baselines_checked or []),
            detectors_run=list(detectors_run or []),
            alarms_fired=alarms_fired,
            next_due=_to_rfc3339(due),
            cadence_seconds=cadence_seconds,
        )
    except ValueError as exc:
        raise AttestationError(str(exc)) from exc


# ── Detecting the run that did not happen ─────────────────────────────────


class OverdueVerdict(BaseModel):
    """Whether the next assurance run is overdue, judged from the last one."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schedule_id: str
    last_ran_at: str
    next_due: str
    checked_at: str
    overdue: bool
    late_by_seconds: int = 0


def check_overdue(
    attestation: AssuranceAttestation, *, now: str
) -> OverdueVerdict:
    """Report whether the next run is overdue, using only the last attestation.

    This is how a run that never happened is detected: it left no record, so the
    verdict is computed against the previous run's ``next_due``.

    Exactly-due is **not** overdue. The boundary matters — a check that fired at
    the instant a run became due would report every on-time schedule as late.
    """
    moment = _parse_rfc3339(now, field="now")
    due = _parse_rfc3339(attestation.next_due, field="next_due")
    late = int((moment - due).total_seconds())
    return OverdueVerdict(
        schedule_id=attestation.schedule_id,
        last_ran_at=attestation.ran_at,
        next_due=attestation.next_due,
        checked_at=_to_rfc3339(moment),
        overdue=late > 0,
        late_by_seconds=max(0, late),
    )


# ── Sealing ───────────────────────────────────────────────────────────────


def into_predicate(attestation: AssuranceAttestation) -> dict[str, Any]:
    """Return the ``extra_predicate`` fragment for the in-toto statement.

    Pass to ``envelopes.intoto.capsule_statement(extra_predicate=...)``. Reusing
    the capsule statement is deliberate: NovaFabric does not introduce a third
    top-level format (ADR-0034).
    """
    return {PREDICATE_KEY: attestation.model_dump(exclude_none=True)}


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], attestation: AssuranceAttestation | None
) -> dict[str, Any]:
    """Attach the attestation additively; returns a new dict."""
    if attestation is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = attestation.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> AssuranceAttestation | None:
    """Read the attestation back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return AssuranceAttestation.model_validate(block)
    except ValueError as exc:
        raise AttestationError(
            f"capsule holds an invalid assurance attestation: {exc}"
        ) from exc
