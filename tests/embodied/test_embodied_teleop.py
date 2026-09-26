"""ADR-0162 P3 — NF-305 teleoperation handoff records.

Acceptance criteria (spec §6 "Teleop handoff"):

- an autonomy→human takeover records ``direction``, a *pseudonymous*
  ``operator_ref`` (no PII), a code-shaped ``trigger`` and a measured
  ``latency_ms``;
- PII-shaped operator refs (e-mail, name with whitespace, phone/badge digit
  run, over-long) are refused by name without echoing the value;
- handoffs are time-ordered (out-of-order refused, ``build_teleop`` sorts
  stably) and bounded in number;
- a repeated direction is a warning, never a refusal;
- the I-2 payload boundary applies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.embodied import (
    EmbodiedFacet,
    HandoffOrderError,
    InvalidLatencyError,
    InvalidTimestampError,
    InvalidTriggerError,
    OperatorIdentityError,
    RawPayloadRejectedError,
    TeleopHandoff,
    build_facet,
    build_teleop,
    check_operator_ref,
    handoff_findings,
    pseudonymize_operator,
)
from novafabric.embodied import teleop as teleop_mod

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "embodied"
OPERATOR = "operator:fp:3f9a1c2e7b4d8a06"


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    raw.pop("_comment", None)
    return raw


def _handoff(**kw: Any) -> TeleopHandoff:
    base: dict[str, Any] = {
        "direction": "autonomy_to_human",
        "operator_ref": OPERATOR,
        "trigger": "odd_exit",
        "latency_ms": 412.5,
        "ts": "2026-07-13T10:02:12Z",
    }
    base.update(kw)
    return TeleopHandoff(**base)


# ── Success ───────────────────────────────────────────────────────────────


def test_takeover_records_every_spec_field() -> None:
    handoff = _handoff()
    dumped = handoff.model_dump()
    assert dumped == {
        "direction": "autonomy_to_human",
        "operator_ref": OPERATOR,
        "trigger": "odd_exit",
        "latency_ms": 412.5,
        "ts": "2026-07-13T10:02:12Z",
    }


def test_integer_latency_is_stored_as_float() -> None:
    assert _handoff(latency_ms=180).latency_ms == 180.0


@pytest.mark.parametrize(
    "ref",
    [
        OPERATOR,
        "human:fp:0123456789abcdef",
        "operator:shift-b.driver-07",
        "did:key:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
    ],
)
def test_pseudonymous_refs_are_accepted(ref: str) -> None:
    assert check_operator_ref(ref) == ref


def test_pseudonymize_operator_is_keyed_stable_and_valid() -> None:
    key = b"k" * 32
    ref = pseudonymize_operator("jane.doe@example.com", key=key)
    assert ref == pseudonymize_operator("jane.doe@example.com", key=key)
    assert ref != pseudonymize_operator("jane.doe@example.com", key=b"j" * 32)
    assert ref.startswith("operator:fp:") and len(ref) == len("operator:fp:") + 16
    assert check_operator_ref(ref) == ref


@pytest.mark.parametrize(
    ("identity", "key"), [("jane", b"short"), ("  ", b"k" * 32)], ids=["short-key", "empty"]
)
def test_pseudonymize_operator_refuses_weak_input(identity: str, key: bytes) -> None:
    with pytest.raises(OperatorIdentityError):
        pseudonymize_operator(identity, key=key)


# ── Pseudonymity (no PII) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("ref", "rule"),
    [
        ("operator:jane.doe@example.com", "e-mail"),
        ("operator:jane＠example.com", "e-mail"),  # full-width @ folds under NFKC
        ("operator:Jane Doe", "personal name"),
        ("human:jane\tdoe", "personal name"),
        ("operator:fp:abc\n", "personal name"),
        ("operator:5551234567", "phone"),
        ("human:555-123-4567", "phone"),
        ("Jane Doe", "personal name"),
        ("jane", "scheme-prefixed"),
        ("agent:planner", "scheme-prefixed"),
        ("operator:ab", "scheme-prefixed"),
        ("operator:" + "a" * 200, "identifier"),
        (42, "string"),
    ],
)
def test_pii_shaped_operator_refs_are_refused_without_echo(ref: Any, rule: str) -> None:
    with pytest.raises(OperatorIdentityError, match=rule) as info:
        _handoff(operator_ref=ref)
    if isinstance(ref, str) and len(ref) > 4:
        assert ref not in str(info.value)


def test_golden_pii_fixture_is_refused() -> None:
    with pytest.raises(OperatorIdentityError):
        EmbodiedFacet.model_validate(_fixture("teleop-pii-operator-facet.json"))


# ── Trigger / latency / ts ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "trigger", ["", "Jane took over after the pedestrian", "ODD_EXIT", "x" * 65, "odd_exit\n", 7]
)
def test_prose_or_oversize_trigger_is_refused(trigger: Any) -> None:
    with pytest.raises(InvalidTriggerError):
        _handoff(trigger=trigger)


@pytest.mark.parametrize("latency", [-1, float("nan"), float("inf"), 86_400_001, True, "412", None])
def test_bad_latency_is_refused(latency: Any) -> None:
    with pytest.raises(InvalidLatencyError):
        _handoff(latency_ms=latency)


@pytest.mark.parametrize("ts", ["2026-07-13T10:02:12", "yesterday", "", None])
def test_naive_or_bad_ts_is_refused(ts: Any) -> None:
    with pytest.raises(InvalidTimestampError):
        _handoff(ts=ts)


def test_unknown_direction_is_refused() -> None:
    with pytest.raises(ValueError, match="direction"):
        _handoff(direction="sideways")


# ── Ordering and bounds ───────────────────────────────────────────────────


def test_build_teleop_sorts_stably_by_instant() -> None:
    late = _handoff(direction="human_to_autonomy", ts="2026-07-13T10:05:40Z")
    first = _handoff(ts="2026-07-13T10:02:12Z", trigger="a")
    same = _handoff(ts="2026-07-13T12:02:12+02:00", trigger="b")  # same instant as `first`
    assert [h.trigger for h in build_teleop([late, first, same])] == ["a", "b", "odd_exit"]


def test_out_of_order_handoffs_are_refused_by_the_facet() -> None:
    raw = _fixture("valid-p3-sim2real-teleop-timing-facet.json")
    raw["teleop"].reverse()
    with pytest.raises(HandoffOrderError, match=r"teleop\[1\]"):
        EmbodiedFacet.model_validate(raw)


def test_handoff_cap_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(teleop_mod, "MAX_HANDOFFS", 2)
    with pytest.raises(HandoffOrderError, match="cap"):
        build_teleop([_handoff(), _handoff(), _handoff()])


def test_direction_repeat_is_a_warning_not_a_refusal() -> None:
    handoffs = build_teleop(
        [
            _handoff(),
            _handoff(ts="2026-07-13T10:03:00Z"),
            _handoff(direction="human_to_autonomy", ts="2026-07-13T10:04:00Z"),
        ]
    )
    findings = handoff_findings(handoffs)
    assert [(f.index, f.code, f.severity) for f in findings] == [(1, "direction_repeat", "warning")]
    assert build_facet(teleop=handoffs) is not None


def test_alternating_directions_have_no_findings() -> None:
    raw = _fixture("valid-p3-sim2real-teleop-timing-facet.json")
    facet = EmbodiedFacet.model_validate(raw)
    assert facet.teleop is not None and handoff_findings(facet.teleop) == []


# ── Boundary + absence ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra", [{"cabin_video": "x"}, {"audio": ""}, {"note": b"\x00"}, {"Raw-Frames": "x"}]
)
def test_payloads_are_refused(extra: dict[str, Any]) -> None:
    with pytest.raises(RawPayloadRejectedError):
        _handoff(**extra)


def test_no_handoffs_writes_no_key() -> None:
    assert build_facet(teleop=[]) is None
