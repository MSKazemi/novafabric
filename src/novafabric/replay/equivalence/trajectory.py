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

"""Tool-trajectory equivalence — ADR-0144 D2/P1 (NF-122).

Compares two canonicalized trajectories under a declared match mode and
tolerance, and reports the divergent steps.

The point of this module, stated plainly: **a dropped or added tool call is a
divergence even when the token stream matched.** That is precisely the failure
a byte-diff of the transcript misses, and the reason behavioral equivalence is
not token equivalence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from novafabric.replay.equivalence.canonicalize import ToolCall


class MatchMode(str, Enum):
    """How strictly the two trajectories must correspond."""

    #: Multiset of calls; order ignored entirely.
    set = "set"
    #: Exact sequence; any difference is total.
    ordered = "ordered"
    #: Levenshtein over call keys; tolerant of localized edits.
    edit = "edit"


class DivergenceKind(str, Enum):
    dropped = "dropped"   # present in the baseline, absent from the replay
    added = "added"       # present in the replay, absent from the baseline
    changed = "changed"   # same position, different call


@dataclass(frozen=True)
class DivergentStep:
    """One difference between the trajectories."""

    kind: DivergenceKind
    #: Position in whichever trajectory contains the call.
    index: int
    baseline: str | None = None
    replay: str | None = None


@dataclass
class TrajectoryComparison:
    """The verdict, its evidence, and the tolerance it was judged against."""

    mode: MatchMode
    #: 0.0 = identical, 1.0 = maximally different.
    distance: float
    tolerance: float
    divergent_steps: list[DivergentStep] = field(default_factory=list)

    @property
    def equivalent(self) -> bool:
        return self.distance <= self.tolerance


def _keys(calls: Sequence[ToolCall]) -> list[str]:
    return [c.key() for c in calls]


def _levenshtein(a: Sequence[str], b: Sequence[str]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,          # deletion
                    current[j - 1] + 1,       # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def _set_divergences(
    baseline: Sequence[ToolCall], replay: Sequence[ToolCall]
) -> list[DivergentStep]:
    """Multiset difference, so a call repeated twice vs once still diverges."""
    # Track (index, key) so that a duplicated call reports the position of the
    # occurrence that is actually unmatched, not the first one with that key.
    unmatched = list(enumerate(_keys(replay)))
    steps: list[DivergentStep] = []
    for i, key in enumerate(_keys(baseline)):
        match = next((pair for pair in unmatched if pair[1] == key), None)
        if match is None:
            steps.append(
                DivergentStep(kind=DivergenceKind.dropped, index=i, baseline=key)
            )
        else:
            unmatched.remove(match)
    steps.extend(
        DivergentStep(kind=DivergenceKind.added, index=j, replay=key)
        for j, key in unmatched
    )
    return steps


def _ordered_divergences(
    baseline: Sequence[ToolCall], replay: Sequence[ToolCall]
) -> list[DivergentStep]:
    b_keys, r_keys = _keys(baseline), _keys(replay)
    steps: list[DivergentStep] = []
    for i in range(max(len(b_keys), len(r_keys))):
        bk = b_keys[i] if i < len(b_keys) else None
        rk = r_keys[i] if i < len(r_keys) else None
        if bk == rk:
            continue
        if bk is None:
            steps.append(DivergentStep(DivergenceKind.added, i, replay=rk))
        elif rk is None:
            steps.append(DivergentStep(DivergenceKind.dropped, i, baseline=bk))
        else:
            steps.append(DivergentStep(DivergenceKind.changed, i, baseline=bk, replay=rk))
    return steps


def compare(
    baseline: Sequence[ToolCall],
    replay: Sequence[ToolCall],
    *,
    mode: MatchMode = MatchMode.ordered,
    tolerance: float = 0.0,
) -> TrajectoryComparison:
    """Compare two canonicalized trajectories.

    Args:
        baseline: the captured trajectory.
        replay: the trajectory produced by the replay.
        mode: which correspondence to require.
        tolerance: maximum distance still counted as equivalent. The default
            is 0.0 — exact — so a caller who wants slack has to say so and
            record how much.

    Note:
        Both trajectories are expected to be canonicalized already
        (``canonicalize.canonicalize``). Comparing raw trajectories works but
        will report formatting differences as divergences.
    """
    b_keys, r_keys = _keys(baseline), _keys(replay)
    longest = max(len(b_keys), len(r_keys))

    if longest == 0:
        # Two empty trajectories are identical. Guarding here rather than
        # dividing by `longest` below, which would be a ZeroDivisionError on
        # the entirely reasonable case of a run that called no tools.
        return TrajectoryComparison(mode=mode, distance=0.0, tolerance=tolerance)

    if mode is MatchMode.set:
        steps = _set_divergences(baseline, replay)
        distance = len(steps) / longest
    elif mode is MatchMode.ordered:
        steps = _ordered_divergences(baseline, replay)
        distance = len(steps) / longest
    else:
        steps = _ordered_divergences(baseline, replay)
        distance = _levenshtein(b_keys, r_keys) / longest

    return TrajectoryComparison(
        mode=mode,
        distance=min(distance, 1.0),
        tolerance=tolerance,
        divergent_steps=steps,
    )
