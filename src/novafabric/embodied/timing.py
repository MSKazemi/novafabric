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

"""Cyber-physical clock & latency evidence — ADR-0162 P3 (NF-308).

``facets.embodied.timing`` records, per clock domain, where its time came
from (``source``: ``ptp`` | ``gps`` | ``monotonic`` | ``system`` |
``declared``), its declared/observed ``offset_ms`` against the reference, and
the ``max_observed_latency_ms`` — so a physical action's timing can be
reasoned about offline. NovaFabric **disciplines no clock**: it never
synchronizes, steers or corrects one, and it does not re-time any other
object in the facet from these numbers.

**One entry per clock domain.** Two entries for ``ptp-0`` would give an
offline reader two answers to "how far off was this clock", so a duplicate is
refused; :func:`build_timing` orders entries by domain so two captures of the
same run produce the same bytes.

**Declared is not verified.** An offset is the operator's claim (ADR-0162
consequences); ``source: declared`` says so explicitly. :func:`timing_findings`
cross-checks only what the capsule itself can show — a sensor stream stamped
against a clock domain the timing block does not describe — and reports it as
a warning, never a refusal.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from novafabric.embodied._boundary import _own_fields, reject_raw_payloads

ClockSource = Literal["ptp", "gps", "monotonic", "system", "declared"]

#: Upper bound on distinct clock domains in one capsule.
MAX_CLOCK_DOMAINS = 256
#: Bounds on recorded milliseconds: one day either way. Beyond that the value
#: is a unit error, not a clock offset or a latency.
MAX_ABS_OFFSET_MS = 86_400_000.0
MAX_LATENCY_MS = 86_400_000.0
MAX_CLOCK_DOMAIN_LEN = 64

#: A clock-domain identifier (``ptp-0``, ``gps/ubx``). Applied with ``fullmatch``.
_CLOCK_DOMAIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}")

FindingCode = Literal["undeclared_clock_domain"]


class InvalidClockDomainError(Exception):
    """Raised when ``clock_domain`` is not a short identifier."""


class InvalidTimingValueError(Exception):
    """Raised when an offset or latency is not a finite, bounded number."""


class ClockDomainConflictError(Exception):
    """Raised on a duplicate clock domain or more than :data:`MAX_CLOCK_DOMAINS`."""


def _bounded_ms(value: Any, *, field: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidTimingValueError(f"{field} must be a number of milliseconds")
    if not math.isfinite(value) or not low <= value <= high:
        raise InvalidTimingValueError(
            f"{field} must be finite and within [{low:.0f}, {high:.0f}] ms; a value "
            "outside that is a unit error, not a measurement"
        )
    return float(value)


class ClockTiming(BaseModel):
    """Timing evidence for one clock domain (NF-308). Recorded, never applied."""

    model_config = ConfigDict(extra="allow")

    clock_domain: str
    source: ClockSource
    #: Declared/observed offset from the reference clock; optional.
    offset_ms: float | None = None
    #: Largest latency observed in this domain.
    max_observed_latency_ms: float

    @field_validator("clock_domain", mode="before")
    @classmethod
    def _check_domain(cls, value: Any) -> Any:
        if not isinstance(value, str) or not _CLOCK_DOMAIN_RE.fullmatch(value):
            raise InvalidClockDomainError(
                f"clock_domain must be an identifier of at most {MAX_CLOCK_DOMAIN_LEN} "
                "characters ([A-Za-z0-9._:/-], no whitespace)"
            )
        return value

    @field_validator("offset_ms", mode="before")
    @classmethod
    def _check_offset(cls, value: Any) -> Any:
        if value is None:
            return None
        return _bounded_ms(value, field="offset_ms", low=-MAX_ABS_OFFSET_MS, high=MAX_ABS_OFFSET_MS)

    @field_validator("max_observed_latency_ms", mode="before")
    @classmethod
    def _check_latency(cls, value: Any) -> Any:
        return _bounded_ms(value, field="max_observed_latency_ms", low=0.0, high=MAX_LATENCY_MS)

    @model_validator(mode="after")
    def _reject_payloads(self) -> ClockTiming:
        reject_raw_payloads(_own_fields(self))
        return self


def check_timing_sequence(entries: Sequence[ClockTiming]) -> None:
    """Refuse duplicate clock domains and an over-long list.

    Raises:
        ClockDomainConflictError: naming the duplicated domain or the cap.
    """
    if len(entries) > MAX_CLOCK_DOMAINS:
        raise ClockDomainConflictError(
            f"{len(entries)} clock domains exceed the {MAX_CLOCK_DOMAINS}-entry cap"
        )
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if entry.clock_domain in seen:
            raise ClockDomainConflictError(
                f"timing[{index}] repeats clock_domain {entry.clock_domain!r}; each "
                "domain is recorded once so its offset has one answer (ADR-0162 NF-308)"
            )
        seen.add(entry.clock_domain)


def build_timing(entries: Iterable[ClockTiming]) -> list[ClockTiming]:
    """Return ``entries`` ordered by ``clock_domain``, checked.

    Raises:
        ClockDomainConflictError: on a duplicate domain or over the cap.
    """
    ordered = sorted(entries, key=lambda e: e.clock_domain)
    check_timing_sequence(ordered)
    return ordered


class TimingFinding(BaseModel):
    """A non-fatal observation about recorded timing evidence."""

    model_config = ConfigDict(frozen=True)

    clock_domain: str
    code: FindingCode
    severity: Literal["warning"] = "warning"
    message: str


def timing_findings(
    entries: Sequence[ClockTiming], sensor_clock_domains: Iterable[str]
) -> list[TimingFinding]:
    """Warn for each sensor clock domain the timing block does not describe.

    ``sensor_clock_domains`` are the ``clock_domain`` values of the capsule's
    NF-301 sensor streams. Deterministic: findings are sorted by domain.
    """
    declared = {entry.clock_domain for entry in entries}
    missing = sorted({d for d in sensor_clock_domains if isinstance(d, str)} - declared)
    return [
        TimingFinding(
            clock_domain=domain[:MAX_CLOCK_DOMAIN_LEN],
            code="undeclared_clock_domain",
            message=(
                f"a sensor stream is stamped against clock domain "
                f"{domain[:MAX_CLOCK_DOMAIN_LEN]!r}, which the timing block does not "
                "describe — its offset and latency are unrecorded"
            ),
        )
        for domain in missing[:MAX_CLOCK_DOMAINS]
    ]


__all__ = [
    "MAX_CLOCK_DOMAINS",
    "ClockDomainConflictError",
    "ClockSource",
    "ClockTiming",
    "InvalidClockDomainError",
    "InvalidTimingValueError",
    "TimingFinding",
    "build_timing",
    "check_timing_sequence",
    "timing_findings",
]
