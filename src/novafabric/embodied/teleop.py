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

"""Teleoperation / human-takeover handoffs — ADR-0162 P3 (NF-305).

``facets.embodied.teleop`` is a time-ordered list of autonomy↔human control
transfers, each ``{direction, operator_ref, trigger, latency_ms, ts}`` — the
labelled, attributable disengagement event 2026 AV regulation expects.
NovaFabric **records** the handoff; it never performs, authorizes, requests or
evaluates a takeover, and it never sits between the operator and the vehicle.

**The operator is pseudonymous, by shape.** ``operator_ref`` must be a
scheme-prefixed identifier (``operator:…``, ``human:…`` or a ``did:…``) and is
refused if it looks like PII: an ``@`` (an e-mail), whitespace (a name), or a
bare run of seven or more digits (a phone or badge number). That is a shape
check, not a proof — ``operator:jane.doe`` is well-formed and still a name —
so :func:`pseudonymize_operator` gives callers a correct way to produce a ref
(a keyed HMAC, never a plain hash an attacker could dictionary-reverse).

**``trigger`` is a code, not prose.** A free-text cause ("Jane took over
after the pedestrian …") is exactly where operator PII and incident narrative
would leak into a sealed capsule; a short machine code (``odd_exit``,
``operator_request``, ``sensor_degraded``) cannot carry either.

**Order is evidence**: handoffs must be in non-decreasing ``ts`` order, and
:func:`build_teleop` sorts them stably. Two consecutive handoffs in the same
direction are *recordable* (a record was probably missed) and surfaced by
:func:`handoff_findings` as a warning, never refused.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.embodied._boundary import _own_fields, reject_raw_payloads
from novafabric.embodied._timestamps import parse_ts

Direction = Literal["autonomy_to_human", "human_to_autonomy"]

#: Upper bound on recorded handoffs in one capsule; far beyond any real run.
MAX_HANDOFFS = 10_000
#: Upper bound on a recorded transfer latency: one day. A larger value is a
#: unit error (µs recorded as ms), not a measurement.
MAX_LATENCY_MS = 86_400_000.0
MAX_OPERATOR_REF_LEN = 160
MAX_TRIGGER_LEN = 64

#: Scheme-prefixed pseudonymous ref. Applied with ``fullmatch``.
_OPERATOR_REF_RE = re.compile(
    r"(?:operator|human):[A-Za-z0-9][A-Za-z0-9._:-]{2,150}"
    r"|did:[a-z0-9]{1,32}:[A-Za-z0-9._:%-]{3,120}"
)
#: Seven or more digits, optionally separated — a phone/badge/ID number.
_DIGIT_RUN_RE = re.compile(r"\d(?:[\s._-]?\d){6,}")
#: A short lower-case machine code. Applied with ``fullmatch``.
_TRIGGER_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}")

FindingCode = Literal["direction_repeat"]


class OperatorIdentityError(Exception):
    """Raised when ``operator_ref`` is not a pseudonymous identity ref.

    Names the rule that fired, never the value — the value is the PII this
    exception exists to keep out of logs as well as capsules (ADR-0009).
    """


class InvalidTriggerError(Exception):
    """Raised when ``trigger`` is not a short machine code."""


class InvalidLatencyError(Exception):
    """Raised when ``latency_ms`` is not a finite, bounded, non-negative number."""


class HandoffOrderError(Exception):
    """Raised when handoffs are out of time order or over :data:`MAX_HANDOFFS`."""


def check_operator_ref(value: Any) -> str:
    """Return ``value`` if it is a pseudonymous operator ref, else raise.

    Raises:
        OperatorIdentityError: naming the rule, never echoing the value.
    """
    if not isinstance(value, str):
        raise OperatorIdentityError("operator_ref must be a string identity ref")
    if len(value) > MAX_OPERATOR_REF_LEN:
        raise OperatorIdentityError(
            f"operator_ref is over {MAX_OPERATOR_REF_LEN} characters; an identity ref "
            "is an identifier, not a document"
        )
    folded = unicodedata.normalize("NFKC", value)
    if "@" in folded:
        raise OperatorIdentityError(
            "operator_ref contains '@' and looks like an e-mail address; teleop "
            "operators are recorded pseudonymously (ADR-0162 NF-305, ADR-0009) — "
            "use pseudonymize_operator()"
        )
    if any(ch.isspace() for ch in folded):
        raise OperatorIdentityError(
            "operator_ref contains whitespace and looks like a personal name; use "
            "a pseudonymous 'operator:…' ref (pseudonymize_operator())"
        )
    if _DIGIT_RUN_RE.search(folded.split(":", 1)[-1]) and not _has_letters_after_scheme(folded):
        raise OperatorIdentityError(
            "operator_ref is a bare run of digits and looks like a phone or badge "
            "number; use a pseudonymous 'operator:fp:…' ref"
        )
    if not _OPERATOR_REF_RE.fullmatch(value):
        raise OperatorIdentityError(
            "operator_ref must be a scheme-prefixed pseudonymous ref: "
            "'operator:<id>', 'human:<id>' or 'did:<method>:<id>' "
            "([A-Za-z0-9._:-], 3+ characters)"
        )
    return value


def _has_letters_after_scheme(value: str) -> bool:
    body = value.split(":", 1)[-1]
    return any(ch.isalpha() for ch in body)


def pseudonymize_operator(identity: str, *, key: bytes) -> str:
    """Derive a stable pseudonymous ``operator:fp:<16 hex>`` ref.

    A keyed HMAC-SHA256, not a plain hash: operator identities are a small,
    guessable space (an employee list), and an unkeyed digest of one is
    re-identifiable by hashing every candidate. The key stays with the
    operator; NovaFabric records only the output.

    Raises:
        OperatorIdentityError: if ``key`` is shorter than 16 bytes or
            ``identity`` is empty.
    """
    if len(key) < 16:
        raise OperatorIdentityError("pseudonymization key must be at least 16 bytes")
    if not identity.strip():
        raise OperatorIdentityError("cannot pseudonymize an empty identity")
    mac = hmac.new(key, identity.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"operator:fp:{mac[:16]}"


class TeleopHandoff(BaseModel):
    """One autonomy↔human control transfer (NF-305). A record, never an act."""

    model_config = ConfigDict(extra="allow")

    direction: Direction
    #: Pseudonymous operator ref — never PII (see :func:`check_operator_ref`).
    operator_ref: str
    #: Declared cause, as a short machine code.
    trigger: str
    #: Measured transfer latency in milliseconds.
    latency_ms: float
    #: Offset-aware ISO-8601 instant, stored as declared.
    ts: str

    @field_validator("operator_ref", mode="before")
    @classmethod
    def _check_operator(cls, value: Any) -> Any:
        return check_operator_ref(value)

    @field_validator("trigger", mode="before")
    @classmethod
    def _check_trigger(cls, value: Any) -> Any:
        if not isinstance(value, str) or not _TRIGGER_RE.fullmatch(value):
            raise InvalidTriggerError(
                f"trigger must be a lower-case machine code of at most {MAX_TRIGGER_LEN} "
                "characters ([a-z0-9_.:-], e.g. 'odd_exit', 'operator_request') — "
                "never prose, which is where operator PII and incident narrative leak"
            )
        return value

    @field_validator("latency_ms", mode="before")
    @classmethod
    def _check_latency(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidLatencyError("latency_ms must be a number of milliseconds")
        if not math.isfinite(value) or value < 0 or value > MAX_LATENCY_MS:
            raise InvalidLatencyError(
                f"latency_ms must be finite and within [0, {MAX_LATENCY_MS:.0f}] "
                "milliseconds; a larger value is a unit error, not a measurement"
            )
        return float(value)

    @field_validator("ts", mode="before")
    @classmethod
    def _check_ts(cls, value: Any) -> Any:
        parse_ts(value)
        return value

    @model_validator(mode="after")
    def _reject_payloads(self) -> TeleopHandoff:
        reject_raw_payloads(_own_fields(self))
        return self


def check_handoff_sequence(handoffs: Sequence[TeleopHandoff]) -> None:
    """Refuse an over-long or out-of-order handoff list.

    Raises:
        HandoffOrderError: naming the offending index.
    """
    if len(handoffs) > MAX_HANDOFFS:
        raise HandoffOrderError(
            f"{len(handoffs)} teleop handoffs exceed the {MAX_HANDOFFS}-record cap"
        )
    previous = None
    for index, handoff in enumerate(handoffs):
        current = parse_ts(handoff.ts)
        if previous is not None and current < previous:
            raise HandoffOrderError(
                f"teleop[{index}] (ts={handoff.ts!r}) precedes the handoff before it; "
                "handoffs must be in non-decreasing time order (ADR-0162 NF-305)"
            )
        previous = current


def build_teleop(handoffs: Iterable[TeleopHandoff]) -> list[TeleopHandoff]:
    """Return ``handoffs`` sorted stably by instant, checked for the cap.

    Raises:
        HandoffOrderError: if there are more than :data:`MAX_HANDOFFS`.
    """
    ordered = sorted(handoffs, key=lambda h: parse_ts(h.ts))
    check_handoff_sequence(ordered)
    return ordered


class TeleopFinding(BaseModel):
    """A non-fatal observation about a recorded handoff sequence."""

    model_config = ConfigDict(frozen=True)

    index: int
    code: FindingCode
    severity: Literal["warning"] = "warning"
    message: str = Field(max_length=512)


def handoff_findings(handoffs: Sequence[TeleopHandoff]) -> list[TeleopFinding]:
    """Warn where two consecutive handoffs run in the same direction.

    Control cannot pass autonomy→human twice without passing back; a repeat
    means a handoff went unrecorded. Recorded evidence, so a warning — never
    a refusal, never an inference about which record is missing.
    """
    findings: list[TeleopFinding] = []
    for index in range(1, min(len(handoffs), MAX_HANDOFFS)):
        if handoffs[index].direction == handoffs[index - 1].direction:
            findings.append(
                TeleopFinding(
                    index=index,
                    code="direction_repeat",
                    message=(
                        f"teleop[{index}] repeats direction {handoffs[index].direction} "
                        "of the handoff before it; a transfer back was not recorded"
                    ),
                )
            )
    return findings


__all__ = [
    "MAX_HANDOFFS",
    "MAX_LATENCY_MS",
    "Direction",
    "HandoffOrderError",
    "InvalidLatencyError",
    "InvalidTriggerError",
    "OperatorIdentityError",
    "TeleopFinding",
    "TeleopHandoff",
    "build_teleop",
    "check_handoff_sequence",
    "check_operator_ref",
    "handoff_findings",
    "pseudonymize_operator",
]
