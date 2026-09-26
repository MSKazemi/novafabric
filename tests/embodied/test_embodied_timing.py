"""ADR-0162 P3 — NF-308 cyber-physical clock & latency evidence.

Acceptance criteria (spec §3 req. 12):

- per clock domain, ``{clock_domain, source, offset_ms, max_observed_latency_ms}``
  records offline with ``offset_ms`` optional;
- ``source`` is the closed spec enum; values are finite and bounded; a domain
  is a bounded identifier; a duplicate domain is refused;
- ``build_timing`` orders by domain (byte-stable) and enforces the cap;
- a sensor stamped against an undescribed domain is a warning, never fatal;
- NovaFabric disciplines no clock: nothing here rewrites another object.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.embodied import (
    ClockDomainConflictError,
    ClockTiming,
    EmbodiedFacet,
    InvalidClockDomainError,
    InvalidTimingValueError,
    RawPayloadRejectedError,
    attach_facet,
    build_facet,
    build_timing,
    timing_findings,
)
from novafabric.embodied import timing as timing_mod

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "embodied"


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    raw.pop("_comment", None)
    return raw


def _clock(**kw: Any) -> ClockTiming:
    base: dict[str, Any] = {
        "clock_domain": "ptp-0",
        "source": "ptp",
        "offset_ms": 0.012,
        "max_observed_latency_ms": 3.4,
    }
    base.update(kw)
    return ClockTiming(**base)


def test_entry_records_spec_fields_and_offset_is_optional() -> None:
    entry = _clock(offset_ms=None)
    assert entry.model_dump(exclude_none=True) == {
        "clock_domain": "ptp-0",
        "source": "ptp",
        "max_observed_latency_ms": 3.4,
    }
    assert _clock(offset_ms=-4).offset_ms == -4.0


@pytest.mark.parametrize("source", ["ptp", "gps", "monotonic", "system", "declared"])
def test_every_spec_source_is_accepted(source: str) -> None:
    assert _clock(source=source).source == source


def test_unknown_source_is_refused() -> None:
    with pytest.raises(ValueError, match="source"):
        _clock(source="ntp-steered")


@pytest.mark.parametrize("domain", ["", "ptp 0", "ptp-0\n", "x" * 65, 3, "-lead"])
def test_bad_clock_domain_is_refused(domain: Any) -> None:
    with pytest.raises(InvalidClockDomainError):
        _clock(clock_domain=domain)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("offset_ms", float("nan")),
        ("offset_ms", 86_400_001),
        ("offset_ms", -86_400_001),
        ("offset_ms", "0.1"),
        ("max_observed_latency_ms", -0.1),
        ("max_observed_latency_ms", float("inf")),
        ("max_observed_latency_ms", True),
        ("max_observed_latency_ms", None),
    ],
)
def test_non_finite_or_out_of_range_values_are_refused(field: str, value: Any) -> None:
    with pytest.raises(InvalidTimingValueError):
        _clock(**{field: value})


def test_build_timing_orders_by_domain() -> None:
    ordered = build_timing([_clock(clock_domain="ptp-0"), _clock(clock_domain="gps-0")])
    assert [e.clock_domain for e in ordered] == ["gps-0", "ptp-0"]


def test_duplicate_domain_is_refused() -> None:
    with pytest.raises(ClockDomainConflictError, match="ptp-0"):
        build_timing([_clock(), _clock(source="declared")])
    with pytest.raises(ClockDomainConflictError):
        EmbodiedFacet.model_validate(_fixture("timing-duplicate-domain-facet.json"))


def test_domain_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(timing_mod, "MAX_CLOCK_DOMAINS", 1)
    with pytest.raises(ClockDomainConflictError, match="cap"):
        build_timing([_clock(clock_domain="a0"), _clock(clock_domain="b0")])


def test_undescribed_sensor_domain_is_a_warning() -> None:
    findings = timing_findings([_clock()], ["ptp-0", "can-bus-1", "can-bus-1", 7])  # type: ignore[list-item]
    assert [(f.clock_domain, f.code, f.severity) for f in findings] == [
        ("can-bus-1", "undeclared_clock_domain", "warning")
    ]
    assert timing_findings([_clock()], ["ptp-0"]) == []


def test_golden_valid_fixture_round_trips_and_is_byte_stable() -> None:
    raw = _fixture("valid-p3-sim2real-teleop-timing-facet.json")
    facet = EmbodiedFacet.model_validate(raw)
    assert facet.timing is not None
    out = attach_facet({"run_id": "r"}, facet)
    assert out["facets"]["embodied"]["timing"] == raw["timing"]
    rebuilt = build_facet(timing=reversed(facet.timing))
    assert rebuilt is not None and rebuilt.timing == facet.timing


def test_payloads_are_refused() -> None:
    with pytest.raises(RawPayloadRejectedError):
        _clock(pps_samples=[1, 2])
    with pytest.raises(RawPayloadRejectedError):
        _clock(trace=b"\x00")


def test_absent_timing_writes_no_key() -> None:
    assert build_facet(timing=[]) is None
