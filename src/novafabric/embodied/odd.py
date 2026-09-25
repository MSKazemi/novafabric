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

"""Safety-envelope / ODD conformance as a *record* — ADR-0162 P2 (NF-303).

``facets.embodied.odd`` carries three things and only three: a digest of the
operational design domain the operator *declared* (``odd_ref``, ISO 34503
taxonomy where present), the excursions outside it that were *observed*, in
time order, and ``verdict: null``.

**The null verdict is the object.** Whether an excursion was unsafe, whether
the system "stayed in-ODD", whether it met UL 4600 / ISO 21448 / ISO 8800 —
those are the operator's assurance case (ADR-0162 D2), never NovaFabric's.
So the model makes a ruling *unrepresentable*: a non-null ``verdict`` and an
excursion marked ``in_odd: true`` are both refused with
:class:`AdjudicationRefusedError`, on every construction path, including
``model_validate`` of a capsule someone else wrote. A reader that finds one in
a capsule has found a record that is not a NovaFabric ODD record.

**Zero excursions is not "stayed in-ODD".** An ``odd`` block with an empty
``excursions`` list says the ODD was declared and no excursion was
*recorded* — which is a statement about the collection process, not about the
vehicle. Every surface that renders this block says so.

**Order is evidence here** (unlike P1's sensor list, which is sorted for byte
stability): excursions must be in non-decreasing ``ts`` order, and
:func:`build_odd` sorts them stably so a caller cannot produce a record that
fails its own reader.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from novafabric.embodied._boundary import _own_fields, _validate_ref, reject_raw_payloads
from novafabric.embodied._timestamps import parse_ts


class AdjudicationRefusedError(Exception):
    """Raised when an ODD record carries a safety or in-ODD ruling (I-4).

    Fires on a non-null ``verdict`` and on an excursion marked
    ``in_odd: true``. Not a ``ValueError``, so pydantic cannot fold it into a
    generic validation error: whoever wrote the ruling — a caller, or a tool
    that rewrote a sealed capsule — must be told by name that NovaFabric
    records declared bounds and observed excursions and never decides whether
    a physical system was safe or inside its operational design domain.
    """

    def __init__(self, field: str, rule: str) -> None:
        super().__init__(
            f"field {field!r} {rule}; facets.embodied.odd is a record, never a "
            "ruling — NovaFabric does not decide whether an excursion was unsafe "
            "or whether the system stayed in-ODD (ADR-0162 D2, I-4)"
        )
        self.field = field
        self.rule = rule


class ExcursionOrderError(Exception):
    """Raised when recorded excursions are not in non-decreasing ``ts`` order.

    Order is the evidence NF-303 adds over a bag of observations: "fog, then
    sensor degradation" and the reverse are different incidents. A list out
    of order was either assembled wrongly or reordered after the fact, and
    neither should be read as the observed sequence.
    """


class OddExcursion(BaseModel):
    """One observed excursion outside the declared ODD (NF-303).

    ``condition`` names the ODD attribute (``fog``, ``night``,
    ``speed_limit``…), ``observed`` the value seen (``visibility<40m``).
    ``in_odd`` is always ``False``: it labels the entry as an observed
    excursion, and is the only value NovaFabric will record.
    """

    model_config = ConfigDict(extra="allow")

    condition: str = Field(min_length=1)
    observed: str = Field(min_length=1)
    #: Offset-aware ISO-8601 instant, stored as declared.
    ts: str
    in_odd: bool = False

    @field_validator("ts", mode="before")
    @classmethod
    def _check_ts(cls, value: Any) -> Any:
        parse_ts(value)
        return value

    @field_validator("in_odd", mode="before")
    @classmethod
    def _refuse_in_odd(cls, value: Any) -> Any:
        # `mode="before"` and a strict identity test: `1`, `"true"` and `True`
        # are all a claim that the system was in-ODD, and none may be coerced
        # past this check by pydantic's lax bool parsing.
        if value is not False:
            raise AdjudicationRefusedError("in_odd", f"is {value!r}, not false")
        return value

    @model_validator(mode="after")
    def _reject_payloads(self) -> OddExcursion:
        reject_raw_payloads(_own_fields(self))
        return self


class OddConformance(BaseModel):
    """The ``facets.embodied.odd`` block: declared ODD + observed excursions.

    ``verdict`` is typed ``None`` and refused by name if anything else is
    offered. It is also *always serialised*, even under ``exclude_none``: the
    spec requires the block to carry ``verdict: null``, and a reader who finds
    the key absent could not tell "never adjudicated" from "adjudication
    stripped".
    """

    model_config = ConfigDict(extra="allow")

    #: ``sha256:`` of the declared ODD specification, held elsewhere.
    odd_ref: str
    excursions: list[OddExcursion] = Field(default_factory=list)
    verdict: None = None

    @field_validator("odd_ref", mode="before")
    @classmethod
    def _check_ref(cls, value: Any) -> Any:
        if value is None or isinstance(value, str):
            return _validate_ref(value)
        reject_raw_payloads(value, path="odd_ref")
        return value

    @field_validator("verdict", mode="before")
    @classmethod
    def _refuse_verdict(cls, value: Any) -> Any:
        if value is not None:
            raise AdjudicationRefusedError("verdict", "is not null")
        return value

    @model_validator(mode="after")
    def _check_order_and_payloads(self) -> OddConformance:
        previous = None
        for index, excursion in enumerate(self.excursions):
            current = parse_ts(excursion.ts)
            if previous is not None and current < previous:
                raise ExcursionOrderError(
                    f"excursions[{index}] (ts={excursion.ts!r}) precedes the "
                    "excursion before it; recorded excursions must be in "
                    "non-decreasing time order (ADR-0162 NF-303)"
                )
            previous = current
        reject_raw_payloads(_own_fields(self))
        return self

    @model_serializer(mode="wrap")
    def _always_carry_null_verdict(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        out: dict[str, Any] = handler(self)
        out["verdict"] = None
        return out


def build_odd(*, odd_ref: str, excursions: Iterable[OddExcursion] = ()) -> OddConformance:
    """Record a declared ODD and its observed excursions.

    Excursions are sorted by instant, stably — equal instants keep the order
    the caller observed them in. The returned block carries ``verdict: None``
    unconditionally; there is no parameter that could set it.

    Raises:
        InvalidReferenceError: if ``odd_ref`` is not a ``sha256:`` digest.
        RawPayloadRejectedError: if any field carries sensor bytes.
    """
    ordered = sorted(excursions, key=lambda e: parse_ts(e.ts))
    return OddConformance(odd_ref=odd_ref, excursions=ordered)


__all__ = [
    "AdjudicationRefusedError",
    "ExcursionOrderError",
    "OddConformance",
    "OddExcursion",
    "build_odd",
]
