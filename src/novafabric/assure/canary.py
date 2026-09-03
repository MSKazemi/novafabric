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

"""Canary-run record — ADR-0147 D3 / NF-153 (evidence half).

Records one canary replay of a pinned baseline against a stack: which baseline,
when, **which stack**, the C3 equivalence verdict, its drift score, and whether
that fires an alarm.

⚠ **This is the record, not the scheduler.** NF-153 also requires re-running each
pinned baseline against the current stack on a declared cadence. That orchestration
needs live infrastructure and is **not built** — ADR-0147's standing production loop
still does not exist. What is here is the evidence object that such a loop would
emit, and which can be produced today from a verdict `nova replay-equivalence check`
already returns.

**Equivalence is not scored here.** NF-153 says so explicitly: it *calls* C3. The
verdict arrives as an input.

## Why `stack_fingerprint` carries the weight

Its job is to answer *"was this canary judged against the same stack as the
baseline?"* — because a verdict compared across two different stacks is not a
comparison, and a "regression" that is really a stack change is a false alarm that
looks exactly like a true one.

Three properties, and the first two are the ones that fail quietly:

- **Deterministic** across processes — a fingerprint that varies per run makes every
  canary look like a stack change.
- **Order-independent** — a stack is a *set* of components. If listing them in a
  different order changed the digest, every canary would look like a regression.
- **Version-sensitive** — any component version change must change the digest, or
  the check passes silently through the exact event it exists to catch.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "canary_run"


class CanaryError(ValueError):
    """A canary-run record could not be built."""


def _parse_rfc3339(value: str, *, field: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CanaryError(
            f"{field} must be an RFC 3339 timestamp, got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise CanaryError(
            f"{field} must carry a UTC offset; a naive timestamp cannot be compared "
            "across hosts"
        )
    return parsed.astimezone(timezone.utc)


def stack_fingerprint(components: Mapping[str, str]) -> str:
    """Digest the stack a canary ran against: component name -> version.

    Order-independent by construction (the mapping is sorted before encoding), so
    the same stack always yields the same digest however it was assembled.

    Raises:
        CanaryError: on an empty stack. A fingerprint over nothing is a constant,
            and would make every stack compare equal — the check would pass while
            comparing nothing at all.
    """
    if not components:
        raise CanaryError(
            "stack has no components; a fingerprint over an empty stack is a "
            "constant and would make every stack compare equal"
        )
    canonical = json.dumps(
        {str(k): str(v) for k, v in components.items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


class CanaryRun(BaseModel):
    """One canary replay of a pinned baseline (NF-153)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = SCHEMA_VERSION
    baseline_id: str
    ran_at: str
    #: Digest of the stack this run executed against.
    stack_fingerprint: str
    #: The stack the baseline was pinned against, when known. Recorded so a
    #: cross-stack comparison is visible rather than silently equated.
    baseline_stack_fingerprint: str | None = None
    #: True when the run executed on the same stack the baseline was pinned on.
    #: None when the baseline's stack is unknown — which is not the same as
    #: "matched", and must not read as it.
    same_stack: bool | None = None
    #: The C3 verdict. Supplied, never computed here (NF-153).
    verdict: bool
    #: C3 distance: 0.0 identical, 1.0 maximally different.
    drift_score: float | None = None
    #: NF-153 alarms on not_equivalent.
    alarm: bool
    components: dict[str, str] = Field(default_factory=dict)

    @field_validator("baseline_id")
    @classmethod
    def _check_baseline_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("baseline_id must not be empty")
        return v


def record_canary_run(
    baseline_id: str,
    *,
    ran_at: str,
    stack: Mapping[str, str],
    equivalent: bool,
    drift_score: float | None = None,
    baseline_stack: Mapping[str, str] | None = None,
) -> CanaryRun:
    """Record one canary replay.

    *equivalent* is the C3 verdict — this function does not score equivalence.
    ``alarm`` is derived from it, never passed in, so the two cannot disagree.
    """
    moment = _parse_rfc3339(ran_at, field="ran_at")
    fingerprint = stack_fingerprint(stack)
    baseline_fp = stack_fingerprint(baseline_stack) if baseline_stack else None

    try:
        return CanaryRun(
            baseline_id=baseline_id,
            ran_at=moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
            stack_fingerprint=fingerprint,
            baseline_stack_fingerprint=baseline_fp,
            same_stack=None if baseline_fp is None else baseline_fp == fingerprint,
            verdict=equivalent,
            drift_score=drift_score,
            # Derived, not supplied: NF-153 alarms on not_equivalent, and an
            # alarm that could disagree with its own verdict is worthless.
            alarm=not equivalent,
            components={str(k): str(v) for k, v in stack.items()},
        )
    except ValueError as exc:
        raise CanaryError(str(exc)) from exc


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(capsule: dict[str, Any], run: CanaryRun | None) -> dict[str, Any]:
    """Attach the canary run additively; returns a new dict."""
    if run is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = run.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> CanaryRun | None:
    """Read the canary run back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return CanaryRun.model_validate(block)
    except ValueError as exc:
        raise CanaryError(f"capsule holds an invalid canary run: {exc}") from exc
