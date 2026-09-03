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

"""Determinism regime — could this replay have been expected to match?

A behavioral-equivalence verdict (ADR-0144) means something different depending
on the regime the original run executed in. A replay that diverges from a run
taken at ``temperature=1.2`` has told you almost nothing; the same divergence
from a fully pinned run is a finding. Until now nothing recorded that difference.

**Relationship to the shipped determinism classifier — read this first.**
``evidence/replay_attestation.py`` (ADR-0094 B, consumed by ``nova replay
--certify``) already classifies a replay as ``BIT_EXACT`` /
``BOUNDED_EQUIVALENT`` / ``NON_DETERMINISTIC``, and it *does* read pins:
``pinned_block_from_capsule()`` plus ``_is_fully_pinned()`` require a
``model_digest``, a ``seed`` and ``lock_mode == "deterministic"``.

That classifier answers *"was this replay reproducible?"* — it needs an
**executed replay** and compares outcome digests. This module answers the
narrower, earlier question *"was the original run's **sampling** regime one where
a replay could be expected to match?"*, from the capsule alone, with **no replay
required**. It also reads **temperature**, which the classifier does not consider
at all.

The two are complementary, not rivals, and they are deliberately kept apart:
folding temperature into ``classify_determinism`` would change what a signed
``ReplayAttestation`` asserts, which is an ADR-level decision about an existing
evidence artifact. ``verdict_for()`` is untouched for the same reason.

Three properties shape the design:

- **Eligibility is tri-state.** ``unknown`` — the run recorded no temperature —
  is not ``eligible``. "We cannot tell" must never read as "it was fine", which
  is the failure mode that makes an assessment worse than none.
- **The facts travel with the verdict.** Per-call temperatures and the counts of
  missing pins are reported alongside the conclusion, so a caller applying a
  looser policy than this module's has the evidence rather than only its opinion.
  Policy hidden inside a boolean cannot be disagreed with.
- **A run with no model calls is ``unknown``.** Vacuous eligibility — "nothing
  was non-deterministic because nothing happened" — is the obvious wrong answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1.0"

#: Capture writes request parameters either as plain body keys or as OpenTelemetry
#: GenAI semantic-convention attributes, depending on the hook that recorded them
#: (``capture/body_adapters`` vs ``capture/hooks/_otel_genai``). Both are read;
#: reading only one would silently report `unknown` for half the capture paths.
_TEMPERATURE_KEYS = ("temperature", "gen_ai.request.temperature")
_SEED_KEYS = ("seed", "gen_ai.request.seed")


class Eligibility(str, Enum):
    """Whether the run was in a regime where a replay could be expected to match."""

    eligible = "eligible"
    not_eligible = "not-eligible"
    #: Not a synonym for eligible. The run did not record what we need to say.
    unknown = "unknown"


class CallRegime(BaseModel):
    """What one model call recorded about its own determinism."""

    model_config = ConfigDict(extra="allow", frozen=True)

    index: int
    temperature: float | None = None
    seed_pinned: bool = False


class DeterminismRegime(BaseModel):
    """The run's regime: the verdict, and the facts behind it."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = SCHEMA_VERSION
    eligibility: Eligibility
    calls: int
    #: Facts, reported independently of the verdict so a caller with a different
    #: policy can reach a different conclusion from the same evidence.
    calls_without_temperature: int = 0
    calls_with_nonzero_temperature: int = 0
    calls_without_seed: int = 0
    temperatures: list[float] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    per_call: list[CallRegime] = Field(default_factory=list)


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    """Read the first present key, searching the record and a nested request."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    request = mapping.get("request")
    if isinstance(request, Mapping):
        for key in keys:
            if key in request:
                return request[key]
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):  # bool is an int; a temperature it is not
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def call_regime(call: Mapping[str, Any], index: int) -> CallRegime:
    """Read one model call's determinism parameters."""
    temperature = _as_float(_first(call, _TEMPERATURE_KEYS))
    seed = _first(call, _SEED_KEYS)
    return CallRegime(
        index=index,
        temperature=temperature,
        seed_pinned=seed is not None and not isinstance(seed, bool),
    )


def assess(calls: Iterable[Mapping[str, Any]]) -> DeterminismRegime:
    """Assess the determinism regime of a run's model calls.

    ``eligible`` requires **every** call to record ``temperature == 0`` and a
    pinned seed — the conservative reading of the replay-attestation schema's
    "missing pins, unpinned seed/temperature ⇒ NON_DETERMINISTIC". A caller who
    treats temperature-0-without-a-seed as good enough has the counts to say so.
    """
    regimes = [call_regime(c, i) for i, c in enumerate(calls)]

    if not regimes:
        return DeterminismRegime(
            eligibility=Eligibility.unknown,
            calls=0,
            reasons=[
                "the run recorded no model calls, so there is nothing to assess; "
                "this is not the same as a deterministic run"
            ],
        )

    no_temp = [r for r in regimes if r.temperature is None]
    nonzero = [r for r in regimes if r.temperature is not None and r.temperature != 0.0]
    no_seed = [r for r in regimes if not r.seed_pinned]

    reasons: list[str] = []
    if nonzero:
        eligibility = Eligibility.not_eligible
        reasons.append(
            f"{len(nonzero)} of {len(regimes)} call(s) ran at a non-zero temperature "
            f"(max {max(r.temperature or 0.0 for r in nonzero)}); a replay is not "
            "expected to match"
        )
    elif no_temp:
        eligibility = Eligibility.unknown
        reasons.append(
            f"{len(no_temp)} of {len(regimes)} call(s) recorded no temperature, so "
            "eligibility cannot be established — this is not evidence of determinism"
        )
    elif no_seed:
        eligibility = Eligibility.not_eligible
        reasons.append(
            f"every call is temperature-0 but {len(no_seed)} of {len(regimes)} pinned "
            "no seed; the replay-attestation schema treats an unpinned seed as "
            "non-deterministic. Callers who accept temperature-0 alone can read "
            "calls_with_nonzero_temperature == 0 instead"
        )
    else:
        eligibility = Eligibility.eligible
        reasons.append(
            f"all {len(regimes)} call(s) are temperature-0 with a pinned seed"
        )

    return DeterminismRegime(
        eligibility=eligibility,
        calls=len(regimes),
        calls_without_temperature=len(no_temp),
        calls_with_nonzero_temperature=len(nonzero),
        calls_without_seed=len(no_seed),
        temperatures=sorted({r.temperature for r in regimes if r.temperature is not None}),
        reasons=reasons,
        per_call=regimes,
    )
