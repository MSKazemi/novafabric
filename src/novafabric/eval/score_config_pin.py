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

"""Score-config aggregation pin (ADR-0117 D4 / P4) for dataset experiments.

Two pure halves:

- :func:`resolve_score_config_pin` — turn a user-supplied ``score_config_ref``
  (``name``, ``name@version``, or ``sha256:<hex>``) into the immutable
  :class:`~novafabric.eval.score_config.ScoreConfig` it names, via a **read-only**
  catalog lookup, and check it actually governs the metric being aggregated. An
  unresolvable, mismatched, or tampered ref is a hard :class:`ScoreConfigPinError`
  raised *before* any item runs — a pin the user asked for is never silently
  dropped.
- :func:`score_config_comparability` — the comparability report keyed by digest:
  same digest ⇒ ``comparable: true``; different digests ⇒ ``comparable: false``
  (both digests + a message); either side unpinned ⇒ ``comparable: null``
  ("not pinned") — comparability is never assumed.

The comparison is over the digests recorded in the two experiment records; it
needs no catalog access, so it works offline on records copied between machines.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from novafabric.eval.experiment import Experiment, ExperimentError
from novafabric.eval.score_config import ScoreConfig, ScoreConfigError
from novafabric.eval.scores import ScoreValueType

__all__ = [
    "MAX_SCORE_CONFIG_REF_LEN",
    "ScoreConfigComparability",
    "ScoreConfigPinError",
    "pinned_ref",
    "resolve_score_config_pin",
    "score_config_comparability",
]

#: Upper bound on a user-supplied ref (bounded input; a real ref is ~70 chars).
MAX_SCORE_CONFIG_REF_LEN = 512


class ScoreConfigPinError(ExperimentError):
    """A requested score-config pin cannot be honoured (unresolvable/mismatched/tampered)."""


def pinned_ref(config: ScoreConfig) -> str:
    """The human-stable ``name@version`` ref recorded for a resolved *config*."""
    return f"{config.name}@{config.version}"


def resolve_score_config_pin(
    ref: str,
    *,
    metric: str,
    value_type: ScoreValueType,
    db_path: Path | None = None,
) -> ScoreConfig:
    """Resolve *ref* to the config that pins aggregates of *metric* (D4).

    Raises :class:`ScoreConfigPinError` when the ref is empty/oversize/malformed,
    names no registered config, resolves to a stored record whose body no longer
    matches its digest, governs a different metric name, or declares a
    ``value_type`` other than the one the experiment's scorer emits.
    """
    from novafabric.eval.score_config_catalog import resolve_config_ref_readonly

    ref = ref.strip()
    if not ref:
        raise ScoreConfigPinError("score config ref must not be empty")
    if len(ref) > MAX_SCORE_CONFIG_REF_LEN:
        raise ScoreConfigPinError(
            f"score config ref is {len(ref)} chars; the limit is {MAX_SCORE_CONFIG_REF_LEN}"
        )
    try:
        config = resolve_config_ref_readonly(ref, db_path=db_path)
    except (ScoreConfigError, ValueError) as exc:
        raise ScoreConfigPinError(f"cannot resolve score config {ref!r}: {exc}") from exc
    if config.name != metric:
        raise ScoreConfigPinError(
            f"score config {pinned_ref(config)} governs metric {config.name!r}, "
            f"but the experiment scores metric {metric!r}"
        )
    if config.value_type is not value_type:
        raise ScoreConfigPinError(
            f"score config {pinned_ref(config)} declares value_type "
            f"{config.value_type.value!r}, but metric {metric!r} is scored as "
            f"{value_type.value!r}"
        )
    return config


class ScoreConfigComparability(BaseModel):
    """Whether two experiments' aggregates of one metric share a pinned definition.

    ``comparable`` is ``True`` (same digest), ``False`` (different digests —
    explicitly not comparable), or ``None`` (at least one side is not pinned for
    this metric; comparability is unknown, never assumed).
    """

    model_config = ConfigDict(extra="forbid")

    comparable: bool | None
    status: Literal["same_digest", "different_digest", "not_pinned"]
    baseline_digest: str | None = None
    candidate_digest: str | None = None
    baseline_ref: str | None = None
    candidate_ref: str | None = None
    message: str


def _ref_name(ref: str | None) -> str | None:
    """Metric name from a recorded ``name@version`` ref (``None`` when absent/unparseable)."""
    if not ref or "@" not in ref:
        return None
    name, _, version = ref.rpartition("@")
    return name if name and version.isdigit() else None


def _side_pin(experiment: Experiment, metric: str) -> tuple[str | None, str | None]:
    """``(digest, ref)`` pinning *metric* on one side, or ``(None, ref)`` if unpinned.

    A digest recorded for a *different* metric (``name@version`` names another
    metric) does not pin *metric*.
    """
    digest = experiment.score_config_digest
    ref = experiment.score_config_ref
    if digest is None:
        return None, ref
    name = _ref_name(ref)
    if name is not None and name != metric:
        return None, ref
    return digest, ref


def score_config_comparability(
    baseline: Experiment, candidate: Experiment, *, metric: str
) -> ScoreConfigComparability:
    """Comparability report keyed by the recorded config digests (ADR-0117 P4)."""
    base_digest, base_ref = _side_pin(baseline, metric)
    cand_digest, cand_ref = _side_pin(candidate, metric)
    common = {
        "baseline_digest": base_digest,
        "candidate_digest": cand_digest,
        "baseline_ref": base_ref,
        "candidate_ref": cand_ref,
    }
    if base_digest is None or cand_digest is None:
        missing = [
            side
            for side, digest in (("baseline", base_digest), ("candidate", cand_digest))
            if digest is None
        ]
        return ScoreConfigComparability(
            comparable=None,
            status="not_pinned",
            message=(
                f"score config not pinned for metric {metric!r} on the "
                f"{' and '.join(missing)} experiment; comparability of the "
                "aggregates is unknown (run with --score-config to pin)"
            ),
            **common,
        )
    if base_digest == cand_digest:
        return ScoreConfigComparability(
            comparable=True,
            status="same_digest",
            message=f"both aggregates of {metric!r} were computed under score config {base_digest}",
            **common,
        )
    return ScoreConfigComparability(
        comparable=False,
        status="different_digest",
        message=(
            f"aggregates of {metric!r} were computed under different score configs "
            f"(baseline {base_ref or '?'} {base_digest} vs candidate "
            f"{cand_ref or '?'} {cand_digest}); they are NOT directly comparable"
        ),
        **common,
    )
