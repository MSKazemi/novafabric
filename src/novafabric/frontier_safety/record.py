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

"""Fail-open capture helpers for P2 frontier-safety objects (ADR-0167 D6, I-2).

The model constructors in :mod:`novafabric.frontier_safety.control` are
*strict*: an invalid control decision raises, because a library caller building
evidence deliberately wants to know. The capture path is the opposite: it runs
next to the very workload the evidence observes, and ADR-0167 is explicit that
safety evidence must **never block** that workload. So these helpers:

- accept a constructed object or a raw mapping from an external protocol/
  framework adapter;
- on *any* invalid material (bad digest, payload-shaped field, inlined C4
  decision, computed verdict, malformed existing facet) log a structured
  warning naming only the error class and field names — never the values,
  which could be the very payload I-5 keeps out — and return the capsule
  **unchanged**;
- never raise, never retry, never do IO.

Nothing here can pause, gate or refuse the run. A fired tripwire recorded here
is only a record that it fired.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from novafabric.frontier_safety._common import FrontierSafetyError
from novafabric.frontier_safety.control import ControlDecision, TripwireTrigger
from novafabric.frontier_safety.facet import (
    FrontierSafetyFacet,
    attach_facet,
    facet_from_capsule,
)

__all__ = ["record_control_decision", "record_tripwire_trigger"]

logger = logging.getLogger(__name__)

#: Errors the capture path absorbs: every way *bad safety material* fails —
#: a named invariant violation, a Pydantic validation failure, or a
#: non-mapping handed in where a record belongs (``TypeError`` from ``dict()``).
#: Deliberately not a bare ``Exception``, so an unrelated bug (say, a
#: ``KeyError`` in this module) is not silently hidden from tests.
_ABSORBED = (FrontierSafetyError, ValidationError, ValueError, TypeError)


def _existing_facet(capsule: Mapping[str, Any]) -> FrontierSafetyFacet:
    """Return the capsule's facet, or an empty one when it has none."""
    return facet_from_capsule(dict(capsule)) or FrontierSafetyFacet()


def _log_dropped(kind: str, exc: BaseException) -> None:
    """Log that a record was dropped, without echoing any field value."""
    fields: list[str] = []
    if isinstance(exc, ValidationError):
        fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
    logger.warning(
        "frontier_safety.%s.dropped",
        kind,
        extra={
            "error_type": type(exc).__name__,
            "fields": fields,
            "fail_open": True,
        },
    )


def record_control_decision(
    capsule: dict[str, Any], decision: ControlDecision | Mapping[str, Any]
) -> dict[str, Any]:
    """Append an external control-protocol decision to the capsule, fail-open.

    Returns a new capsule dict with the decision appended to
    ``facets.frontier_safety.control_decisions``. On invalid material the
    input capsule is returned unchanged and a warning is logged; this function
    never raises for bad safety material and never blocks the workload
    (ADR-0167 I-2).
    """
    try:
        record = (
            decision
            if isinstance(decision, ControlDecision)
            else ControlDecision.model_validate(dict(decision))
        )
        facet = _existing_facet(capsule)
        facet.control_decisions = [*(facet.control_decisions or []), record]
        return attach_facet(capsule, facet)
    except _ABSORBED as exc:
        _log_dropped("control_decision", exc)
        return capsule


def record_tripwire_trigger(
    capsule: dict[str, Any], trigger: TripwireTrigger | Mapping[str, Any]
) -> dict[str, Any]:
    """Append a fired published indicator to the capsule, fail-open.

    Returns a new capsule dict with the trigger appended to
    ``facets.frontier_safety.tripwire_triggers``. Recording that a tripwire
    fired applies no safeguard and blocks nothing; on invalid material the
    input capsule is returned unchanged and a warning is logged (ADR-0167
    I-1, I-2).
    """
    try:
        record = (
            trigger
            if isinstance(trigger, TripwireTrigger)
            else TripwireTrigger.model_validate(dict(trigger))
        )
        facet = _existing_facet(capsule)
        facet.tripwire_triggers = [*(facet.tripwire_triggers or []), record]
        return attach_facet(capsule, facet)
    except _ABSORBED as exc:
        _log_dropped("tripwire_trigger", exc)
        return capsule
