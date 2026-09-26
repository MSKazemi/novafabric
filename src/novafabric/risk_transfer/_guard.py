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

"""Shared validation controls for the risk-transfer facet (ADR-0170).

Three controls every ``facets.risk_transfer`` object shares, kept in one place
so the P1 and P2 objects cannot drift apart:

- **References** — a reference is a ``sha256:<64 hex>`` digest, matched with
  :func:`re.fullmatch` (a ``$``-anchored pattern would accept a trailing
  newline, i.e. a reference that is not the digest it looks like).
- **Declared decimals** — thresholds and observed values are
  :class:`~decimal.Decimal`, never ``float``: a comparison against a declared
  threshold of ``99.9`` must not flip on binary rounding.
- **The determination guard** — ADR-0170 I-4: NovaFabric records evidence and
  never a verdict. Every object here is ``extra="allow"`` (the facet pattern),
  so the structural shape alone cannot keep a ``fault``/``payout``/
  ``claim_decision`` key out; :func:`reject_determination_fields` walks the
  whole payload, normalises each key (case, separators) and rejects any key
  containing a determination marker as a substring. It also bounds depth,
  list length and free-string length, so an open ``extra`` field cannot be
  used to smuggle a narrative or an oversize blob into the sealed record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

#: Longest free string any risk-transfer field may carry (extra fields
#: included). Long enough for a label or a short URI-free basis note, far too
#: short for an incident narrative.
MAX_FREE_STRING = 256
#: Deepest nesting an ``extra`` payload may use.
MAX_DEPTH = 12
#: Longest list anywhere in a payload.
MAX_LIST = 256
#: Longest textual form accepted for a declared decimal.
MAX_DECIMAL_CHARS = 64

#: Substring markers of a *determination* — a finding of fault, a coverage or
#: claim decision, a payout, a remedy, a price. Matched against keys
#: normalised to ``[a-z0-9]`` (so ``At-Fault``, ``at_fault`` and ``atFault``
#: all read ``atfault``). Chosen so no legitimate ADR-0170 field contains one:
#: ``covered_event_kind`` and ``liability_chain`` are fine, ``is_covered`` and
#: ``liability_share`` are not.
DETERMINATION_MARKERS: tuple[str, ...] = (
    "fault",
    "verdict",
    "liable",
    "culpab",
    "negligen",
    "blame",
    "guilt",
    "responsib",
    "apportion",
    "adjudic",
    "determination",
    "ruling",
    "judgment",
    "judgement",
    "liabilityshare",
    "liabilitypercent",
    "liabilityassign",
    "liabilityfinding",
    "payout",
    "payable",
    "indemn",
    "settlementamount",
    "settledamount",
    "remedy",
    "remedies",
    "damages",
    "servicecredit",
    "penalt",
    "compensat",
    "reimburs",
    "claimdecision",
    "claimstatus",
    "claimapproved",
    "claimdenied",
    "approv",
    "denied",
    "denial",
    "iscovered",
    "coverageapplies",
    "coveragedecision",
    "coveragegranted",
    "coverageconfirmed",
    "policyresponds",
    "premium",
    "underwrit",
    "riskscore",
    "riskrating",
)


class InvalidReferenceError(Exception):
    """Raised when a reference is not a ``sha256:`` digest.

    Deliberately **not** a ``ValueError``. Pydantic v2 catches ``ValueError``
    inside a validator and folds it into a ``ValidationError`` alongside
    ordinary shape complaints, destroying the named type — and the most likely
    bad reference here is an inlined DFIR excerpt or a claim number, which the
    caller must be told about specifically.
    """


class DeterminationFieldRejectedError(Exception):
    """Raised when a payload carries a verdict-shaped key (ADR-0170 I-4).

    NovaFabric records *who was attributed* and *what was observed against a
    declared threshold*; a key that reads as a finding of fault, a coverage or
    claim decision, a payout or a remedy is refused rather than recorded.
    Names the path and the marker, never the value.
    """

    def __init__(self, path: str, marker: str) -> None:
        super().__init__(
            f"field {path!r} is determination-shaped (marker {marker!r}); "
            "NovaFabric records evidence and never a fault, coverage, claim or "
            "payout determination (ADR-0170 I-4)"
        )
        self.path = path
        self.marker = marker


class UnboundedFieldError(Exception):
    """Raised when a payload exceeds a depth, list-length or string-length cap."""


class InvalidDecimalError(Exception):
    """Raised when a threshold or observed value is not an exact finite decimal.

    ``float`` is refused by type (binary rounding would make a comparison
    against a declared threshold unstable), as are ``bool``, NaN and infinities
    (a comparison against NaN is always false and would read as "no breach").
    """


def normalise_key(key: object) -> str:
    """Return ``key`` lower-cased with every non-alphanumeric removed."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def validate_ref(value: str | None) -> str | None:
    """Return ``value`` if it is ``None`` or a ``sha256:<64 hex>`` digest.

    Raises:
        InvalidReferenceError: for anything else.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise InvalidReferenceError(
            f"reference {str(value)[:80]!r} is not a 'sha256:<64 hex>' digest; "
            "narrative text, identifiers and URIs are never stored here "
            "(ADR-0170, I-2)"
        )
    return value


def validate_refs(values: list[str], *, limit: int = MAX_LIST) -> list[str]:
    """Validate a bounded list of digests; see :func:`validate_ref`."""
    if len(values) > limit:
        raise UnboundedFieldError(f"reference list has {len(values)} entries (cap {limit})")
    for ref in values:
        validate_ref(ref)
    return values


def reject_determination_fields(value: Any, *, path: str = "", depth: int = 0) -> None:
    """Walk ``value``; raise on a verdict-shaped key or an unbounded field.

    Raises:
        DeterminationFieldRejectedError: a key contains a determination marker.
        UnboundedFieldError: nesting, a list, or a string exceeds its cap.
    """
    if depth > MAX_DEPTH:
        raise UnboundedFieldError(f"{path or '<root>'}: nesting deeper than {MAX_DEPTH}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            if len(name) > MAX_FREE_STRING:
                raise UnboundedFieldError(f"{child_path[:80]}: key longer than cap")
            norm = normalise_key(name)
            for marker in DETERMINATION_MARKERS:
                if marker in norm:
                    raise DeterminationFieldRejectedError(child_path, marker)
            reject_determination_fields(child, path=child_path, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST:
            raise UnboundedFieldError(f"{path}: list has {len(value)} entries (cap {MAX_LIST})")
        for index, item in enumerate(value):
            reject_determination_fields(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str) and len(value) > MAX_FREE_STRING:
        raise UnboundedFieldError(
            f"{path}: string of {len(value)} chars exceeds cap {MAX_FREE_STRING}"
        )


def to_decimal(value: Any) -> Decimal:
    """Coerce a declared/observed figure to an exact finite :class:`Decimal`.

    Accepts ``Decimal``, ``int`` (not ``bool``) and a decimal string. JSON
    numbers should be parsed with ``json.loads(..., parse_float=Decimal)`` so
    they arrive exact.

    Raises:
        InvalidDecimalError: for ``float``, ``bool``, NaN/infinity, an
            unparsable or over-long string, or any other type.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise InvalidDecimalError(
            f"{type(value).__name__} {value!r} is not an exact decimal; pass a "
            "Decimal, an int or a decimal string (ADR-0170: no float thresholds)"
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        if len(value) > MAX_DECIMAL_CHARS:
            raise InvalidDecimalError(f"decimal string longer than {MAX_DECIMAL_CHARS} chars")
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise InvalidDecimalError(f"{value!r} is not a decimal number") from exc
    else:
        raise InvalidDecimalError(f"{type(value).__name__} is not a decimal number")
    if not result.is_finite():
        raise InvalidDecimalError(f"{value!r} is not finite")
    if len(str(result)) > MAX_DECIMAL_CHARS:
        raise InvalidDecimalError(f"decimal longer than {MAX_DECIMAL_CHARS} chars")
    return result


#: A Decimal field: strict on input (see :func:`to_decimal`), serialised as a
#: string in every dump mode so a capsule dict stays YAML/JSON-safe and exact.
ExactDecimal = Annotated[
    Decimal,
    BeforeValidator(to_decimal),
    PlainSerializer(lambda d: str(d), return_type=str),
]

#: Lower-case machine label (metric, marker, event kind).
LABEL_PATTERN = r"[a-z0-9][a-z0-9_.:-]{0,63}"


def check_label(value: str, *, what: str, pattern: str = LABEL_PATTERN) -> str:
    """Return ``value`` if it fully matches ``pattern``; else raise ``ValueError``."""
    if not re.fullmatch(pattern, value):
        raise ValueError(f"{what} {value[:80]!r} does not match {pattern}")
    return value


__all__ = [
    "DETERMINATION_MARKERS",
    "LABEL_PATTERN",
    "MAX_DECIMAL_CHARS",
    "MAX_DEPTH",
    "MAX_FREE_STRING",
    "MAX_LIST",
    "DeterminationFieldRejectedError",
    "ExactDecimal",
    "InvalidDecimalError",
    "InvalidReferenceError",
    "UnboundedFieldError",
    "check_label",
    "normalise_key",
    "reject_determination_fields",
    "to_decimal",
    "validate_ref",
    "validate_refs",
]
