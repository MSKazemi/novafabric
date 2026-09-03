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

"""Production regression alarm — ADR-0147 D4 / NF-156.

Runs the shipped Wilson + Wald-SPRT primitive (`eval.regression_diff`, NF-007,
ADR-0080) over a window of run outcomes and emits a three-valued verdict:
``no_regression`` / ``regression`` / ``inconclusive``. It **fires only on
ACCEPT_H1**.

**Why reuse the primitive rather than threshold a delta.** Agentic pass@1 swings
several points as noise even at temperature 0, so a bare delta gate fires on noise.
The ADR's phrasing is the requirement: *a single-run dip must not fire an alarm*.
``CONTINUE`` — not enough evidence yet — is therefore ``inconclusive`` and does
**not** fire; treating "not yet decided" as "regression" is exactly the false alarm
the SPRT exists to prevent.

**Polarity is the trap here.** ``significance_diff`` reads ``1`` as *pass*, while
ADR-0147 offers the window as *"the drifted/not (or pass/fail) sequence"* — and
those are opposite. A drifted-as-1 sequence fed straight in makes the alarm fire on
*improvement* and stay silent on *regression*, with every number in the output still
looking plausible. So the input here is named for its polarity (``1 = healthy``) and
inversion is an explicit argument, not something the caller must remember.

**This is not the promote gate.** ADR-0147 D4 says so directly: *"a standing alarm,
not a second promote gate — ADR-0080 is unchanged"*. It deliberately does not use
``SignificanceDiff.exit_code()``, whose ``3`` is the promote gate's contract. An
alarm that exits with the gate's code is a gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from novafabric.eval.regression_diff import significance_diff
from novafabric.eval.significance import SprtVerdict

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "regression_alarm"


class AlarmError(ValueError):
    """A regression alarm could not be evaluated."""


class AlarmVerdict(str, Enum):
    """The three-valued verdict (ADR-0147 D4)."""

    no_regression = "no-regression"
    regression = "regression"
    inconclusive = "inconclusive"


#: SPRT verdict -> alarm verdict. Only ACCEPT_H1 fires.
_VERDICT_MAP = {
    SprtVerdict.ACCEPT_H0: AlarmVerdict.no_regression,
    SprtVerdict.ACCEPT_H1: AlarmVerdict.regression,
    SprtVerdict.CONTINUE: AlarmVerdict.inconclusive,
}


class RegressionAlarm(BaseModel):
    """The alarm's verdict, and the parameters that produced it."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = SCHEMA_VERSION
    metric: str
    verdict: AlarmVerdict
    #: True only for ``regression``. An alarm that fired on ``inconclusive``
    #: would be the noise-triggered gate this design exists to avoid.
    fired: bool
    baseline_n: int
    window_n: int
    baseline_successes: int
    window_successes: int
    #: Recorded so the verdict is reproducible: the same window judged under
    #: different p0/p1/alpha/beta is a different verdict.
    sprt: dict[str, Any] = Field(default_factory=dict)


def _validate(outcomes: Sequence[int], *, field: str) -> list[int]:
    if not outcomes:
        raise AlarmError(f"{field} must not be empty")
    bad = {o for o in outcomes if o not in (0, 1)}
    if bad:
        raise AlarmError(
            f"{field} must contain only 0 or 1; got {sorted(bad)!r}. "
            "An outcome sequence is Bernoulli — a score belongs in the numeric "
            "drift signal, not here."
        )
    return list(outcomes)


def evaluate(
    baseline_outcomes: Sequence[int],
    window_outcomes: Sequence[int],
    *,
    metric: str = "task_pass",
    outcomes_are_drift_flags: bool = False,
    **sprt_kwargs: Any,
) -> RegressionAlarm:
    """Evaluate the standing regression alarm over a window.

    Args:
        baseline_outcomes: the pinned baseline's outcomes, ``1 = healthy``.
        window_outcomes: the production window's outcomes, ``1 = healthy``.
        metric: what the outcomes measure.
        outcomes_are_drift_flags: set when the sequences are ``1 = drifted``
            rather than ``1 = healthy``; both are inverted before scoring.
            Explicit because feeding drift flags in unconverted inverts the
            alarm silently — it would fire on improvement and stay quiet on a
            real regression.
        **sprt_kwargs: passed through to ``significance_diff`` (``p0``, ``p1``,
            ``alpha``, ``beta``, ``confidence``).
    """
    base = _validate(baseline_outcomes, field="baseline_outcomes")
    window = _validate(window_outcomes, field="window_outcomes")

    if outcomes_are_drift_flags:
        base = [1 - o for o in base]
        window = [1 - o for o in window]

    try:
        diff = significance_diff(base, window, metric=metric, **sprt_kwargs)
    except (TypeError, ValueError) as exc:
        raise AlarmError(f"cannot evaluate the alarm: {exc}") from exc

    verdict = _VERDICT_MAP[diff.sprt.verdict]
    return RegressionAlarm(
        metric=metric,
        verdict=verdict,
        fired=verdict is AlarmVerdict.regression,
        baseline_n=diff.baseline.n,
        window_n=diff.candidate.n,
        baseline_successes=diff.baseline.successes,
        window_successes=diff.candidate.successes,
        sprt=diff.sprt.model_dump(mode="json"),
    )


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], alarm: RegressionAlarm | None
) -> dict[str, Any]:
    """Attach the alarm additively; returns a new dict."""
    if alarm is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = alarm.model_dump(mode="json", exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> RegressionAlarm | None:
    """Read the alarm back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return RegressionAlarm.model_validate(block)
    except ValueError as exc:
        raise AlarmError(f"capsule holds an invalid regression alarm: {exc}") from exc
