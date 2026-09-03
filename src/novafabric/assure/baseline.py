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

"""Golden baseline pins — ADR-0147 D1/P1 (NF-160).

ADR-0147 states the reason this module exists before any detector does:
*"A drift loop is meaningless without a fixed reference."* Every detector and
canary in D2–D7 measures **against a pinned baseline**, never against a moving
average alone, so a baseline that can drift is not a baseline.

Four properties carry that weight:

- **Immutable.** A pin is never edited. :func:`supersede` returns a *new* pin
  naming its predecessor, and the model itself is frozen, so "immutable" is a
  property the type enforces rather than a convention a caller is asked to keep.
  A mutable baseline silently redefines what every past comparison meant.
- **Bound to a sealed root.** Each pinned run carries the ``sha256:`` Merkle root
  of its sealed capsule, so a pin names *specific bytes*, not a run id that could
  later point at different content.
- **Re-verifiable offline.** :func:`verify_pin` recomputes the root from the
  capsule on disk and reports agreement. It reports; it never rewrites the pin to
  match what it found — that would "repair" the record by redefining the object,
  turning a detected mismatch into a silently accepted new baseline. (Same
  discipline as ``preservation.anchor.check_fixity``.)
- **Fail-open and additive.** A capsule with no baseline is returned
  byte-identical; absence is never an exception and never blocks a workload.

Scope of this slice: the pin object, its facet, and offline re-verification.
Listing pins across a fleet needs a global registry, which is a storage decision
of its own and is deliberately not invented here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from novafabric._hashutil import InvalidDigestError, validate_digest
from novafabric.evidence.merkle import capsule_merkle_root

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "baseline"

#: The C3 criterion vocabulary (ADR-0147 D1). A pin declares which axis it is a
#: baseline *for*; comparing a cost baseline against a goal criterion is a
#: category error, so the vocabulary is closed rather than free text.
Criterion = Literal["goal", "trajectory", "output-dist", "cost"]

CRITERIA: tuple[str, ...] = ("goal", "trajectory", "output-dist", "cost")


class BaselineError(Exception):
    """A baseline pin could not be built, read, or verified."""


class ImmutableBaselineError(BaselineError):
    """An attempt was made to modify a pinned baseline."""


class BaselineRun(BaseModel):
    """One golden capsule inside a pin: a run id bound to its sealed root."""

    model_config = ConfigDict(extra="allow", frozen=True)

    run_id: str
    #: ``sha256:`` Merkle root of the sealed capsule, per ADR-0147 D1.
    baseline_root: str

    @field_validator("baseline_root", mode="before")
    @classmethod
    def _check_root(cls, v: object) -> str:
        try:
            return validate_digest(v, field="baseline_root")
        except InvalidDigestError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("run_id must not be empty")
        return v


class BaselinePin(BaseModel):
    """An immutable designation of one or more golden capsules (NF-160).

    ``frozen=True`` is the enforcement of ADR-0147's immutability requirement.
    ``extra="allow"`` lets the later phases (canary schedules, impact reports)
    add fields without a schema break.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: str = SCHEMA_VERSION
    baseline_id: str
    runs: list[BaselineRun] = Field(min_length=1)
    criterion: Criterion
    pinned_at: str
    #: Always true. Serialised explicitly because a reader of the JSON should not
    #: have to know this module's rules to learn that the record cannot be edited.
    immutable: bool = True
    #: Set only on a pin produced by :func:`supersede`.
    supersedes: str | None = None
    cost_profile: dict[str, Any] | None = None
    tool_mix: dict[str, float] | None = None

    @field_validator("baseline_id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("baseline_id must not be empty")
        return v

    @field_validator("immutable")
    @classmethod
    def _check_immutable(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError(
                "a baseline pin is immutable by definition; immutable=False is not "
                "a supported state (ADR-0147 D1)"
            )
        return v


# ── Construction ──────────────────────────────────────────────────────────


def pin_baseline(
    baseline_id: str,
    runs: list[BaselineRun] | list[dict[str, Any]],
    criterion: str,
    *,
    pinned_at: str,
    cost_profile: dict[str, Any] | None = None,
    tool_mix: dict[str, float] | None = None,
) -> BaselinePin:
    """Build a baseline pin.

    Raises:
        BaselineError: if *criterion* is outside the C3 vocabulary, or a run's
            ``baseline_root`` is not a canonical digest.
    """
    if criterion not in CRITERIA:
        raise BaselineError(
            f"unknown criterion {criterion!r}; expected one of {', '.join(CRITERIA)}"
        )
    try:
        return BaselinePin(
            baseline_id=baseline_id,
            runs=[
                r if isinstance(r, BaselineRun) else BaselineRun.model_validate(r)
                for r in runs
            ],
            criterion=criterion,  # type: ignore[arg-type]
            pinned_at=pinned_at,
            cost_profile=cost_profile,
            tool_mix=tool_mix,
        )
    except ValueError as exc:
        raise BaselineError(str(exc)) from exc


def baseline_run_from_capsule(run_id: str, capsule_dir: Path) -> BaselineRun:
    """Bind *run_id* to the sealed Merkle root of the capsule at *capsule_dir*."""
    try:
        root = capsule_merkle_root(capsule_dir)
    except Exception as exc:
        raise BaselineError(
            f"cannot compute the sealed root of {capsule_dir}: {exc}"
        ) from exc
    return BaselineRun(run_id=run_id, baseline_root=_as_prefixed(root))


def supersede(
    previous: BaselinePin,
    runs: list[BaselineRun] | list[dict[str, Any]],
    *,
    pinned_at: str,
    baseline_id: str | None = None,
    criterion: str | None = None,
    cost_profile: dict[str, Any] | None = None,
    tool_mix: dict[str, float] | None = None,
) -> BaselinePin:
    """Return a NEW pin that supersedes *previous*.

    This is the only supported way to change what a baseline points at. The
    previous pin is returned untouched, so a comparison made against it remains
    reproducible after the baseline moves on — which is the whole point of
    pinning (ADR-0147 D1).
    """
    new = pin_baseline(
        baseline_id or previous.baseline_id,
        runs,
        criterion or previous.criterion,
        pinned_at=pinned_at,
        cost_profile=cost_profile,
        tool_mix=tool_mix,
    )
    return new.model_copy(update={"supersedes": previous.baseline_id})


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(
    capsule: dict[str, Any], pin: BaselinePin | None
) -> dict[str, Any]:
    """Attach *pin* to a capsule dict additively; returns a new dict.

    Writes nothing when *pin* is None: a run with no baseline must be
    byte-identical to one captured before this feature existed.
    """
    if pin is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = pin.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> BaselinePin | None:
    """Read a baseline pin back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return BaselinePin.model_validate(block)
    except ValueError as exc:
        raise BaselineError(f"capsule holds an invalid baseline facet: {exc}") from exc


# ── Offline re-verification ───────────────────────────────────────────────


class BaselineVerification(BaseModel):
    """The result of re-verifying one pinned run against a capsule on disk."""

    model_config = ConfigDict(extra="allow", frozen=True)

    run_id: str
    pinned_root: str
    observed_root: str
    matches: bool


def verify_pin(
    pin: BaselinePin, capsule_dirs: dict[str, Path]
) -> list[BaselineVerification]:
    """Recompute each pinned run's sealed root and report agreement.

    *capsule_dirs* maps ``run_id`` to the capsule directory to re-hash. A run not
    present in the mapping is skipped rather than reported as a mismatch: a check
    that could not run did not find corruption, and recording it as ``matches:
    false`` would fabricate a finding.

    The pin is never modified — not even on a mismatch. See the module docstring.
    """
    results: list[BaselineVerification] = []
    for run in pin.runs:
        directory = capsule_dirs.get(run.run_id)
        if directory is None:
            continue
        try:
            observed = _as_prefixed(capsule_merkle_root(directory))
        except Exception as exc:
            raise BaselineError(
                f"cannot recompute the sealed root of {directory}: {exc}"
            ) from exc
        results.append(
            BaselineVerification(
                run_id=run.run_id,
                pinned_root=run.baseline_root,
                observed_root=observed,
                matches=observed == run.baseline_root,
            )
        )
    return results


def _as_prefixed(root: str) -> str:
    """Normalise a Merkle root to the canonical ``sha256:`` form."""
    return root if root.startswith("sha256:") else f"sha256:{root}"
