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

"""Computational-reproducibility receipt — ADR-0164 D2/P2 (NF-323).

A :class:`ReproducibilityReceipt` records *what would have to match* for a third
party to re-run a piece of agentic science: the environment digest (container or
lockfile), the RNG seed(s), the input-data digest, the code digest, an optional
workflow digest, and a declared ``determinism_class``. Every present component is
content-addressed together under one ``bound_root`` (NF-321-330 spec §3 item 7),
so changing any one of them — or the sealed capsule root the receipt names —
changes the root, and :func:`verify_receipt` detects it offline.

It extends the NF-023 eval-closure manifest
(:mod:`novafabric.eval.provenance_manifest`) from *eval* suites to *research*
workloads, with the same stance: a pure hashing / comparison check.

Three rules shape everything here:

- **Record-only.** The receipt never re-executes anything and never asserts the
  run *is* reproducible. :class:`ReceiptVerification` carries
  ``re_executed=False`` and ``reproducible_in_fact=None`` as fixed values so no
  caller can read "verified" as "reproduced" (I-4).
- **Never fabricated.** A component the producer did not supply is *absent* and
  named in ``receipt_incomplete`` — it is never guessed, defaulted, or derived
  from something else (spec §3 item 7).
- **No payloads.** Digest fields accept ``sha256:<64 hex>`` only; raw bytes or
  over-long strings raise :class:`~novafabric.science.provenance.PayloadCaptureError`
  (I-2, ADR-0021 §4, ADR-0009).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from novafabric._hashutil import sha256_prefixed
from novafabric.science.provenance import (
    FACET_NAME,
    ScienceProvenanceError,
    _validate_digest,
)

__all__ = [
    "IN_MISSION_BOUNDARY",
    "MAX_SEEDS",
    "RECEIPT_KEY",
    "RECEIPT_SCHEMA_VERSION",
    "REQUIRED_COMPONENTS",
    "DeterminismClass",
    "InvalidSeedError",
    "ReceiptVerification",
    "ReproducibilityReceipt",
    "ReproducibilityReceiptError",
    "attach_receipt",
    "build_receipt",
    "compute_bound_root",
    "missing_components",
    "receipt_from_capsule",
    "verify_receipt",
]

#: Key of the receipt inside ``facets.science_provenance``. The P1 facet model is
#: ``extra="allow"``, which is what lets this land without a schema change.
RECEIPT_KEY = "reproducibility_receipt"
RECEIPT_SCHEMA_VERSION = "0.1.0"

#: Domain-separation tag hashed into every root, so a receipt root can never
#: collide with a digest some other facet computes over a similar-looking dict.
_ROOT_DOMAIN = "novafabric.science.reproducibility_receipt/v1"

#: The one-line boundary every science CLI output carries (spec §3 item 4).
IN_MISSION_BOUNDARY = (
    "NovaFabric records, verifies and exports the provenance of agentic science; it "
    "never runs experiments, re-executes computations, or adjudicates scientific "
    "validity. Evidence supports reproducibility; it does not guarantee it."
)

DeterminismClass = Literal["bitwise", "statistical", "nondeterministic", "undeclared"]

#: Components whose absence makes a receipt incomplete, in reporting order.
#: ``workflow_digest`` is deliberately not here: the spec marks it optional, and a
#: plain script run has no workflow to digest — calling that "incomplete" would
#: push producers to invent one.
REQUIRED_COMPONENTS: tuple[str, ...] = (
    "environment_digest",
    "seeds",
    "data_digest",
    "code_digest",
)

#: Bound on the seed list, so an offline verifier walking an untrusted capsule
#: does bounded work (the repo's bounded-everything rule).
MAX_SEEDS = 1024

# Seeds are recorded as integers in the widest range any mainstream RNG accepts:
# signed 64-bit (Java/JAX-style) through unsigned 64-bit (NumPy SeedSequence
# entropy words). Anything outside is not a seed; it is data.
_SEED_MIN = -(2**63)
_SEED_MAX = 2**64 - 1


# ── Errors ────────────────────────────────────────────────────────────────


class ReproducibilityReceiptError(ScienceProvenanceError):
    """Base class for every reproducibility-receipt error."""


class InvalidSeedError(ReproducibilityReceiptError):
    """Raised when a seed is not an integer, is out of range, or seeds are too many."""


# ── Validation helpers ────────────────────────────────────────────────────


def _validate_seeds(value: object) -> list[int]:
    """Normalise ``seed`` / ``seeds`` input to an ordered list of integers.

    A scalar becomes a one-element list; order is preserved because the spec
    calls it an *ordered* ``seeds`` list — seed 0 of a run and seed 1 are not
    interchangeable. ``bool`` is rejected even though it subclasses ``int``:
    ``True`` as a seed is a caller bug, never an intent.
    """
    if value is None:
        return []
    items: list[object]
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    if len(items) > MAX_SEEDS:
        raise InvalidSeedError(f"{len(items)} seeds exceeds the {MAX_SEEDS}-seed limit")
    out: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, int):
            raise InvalidSeedError(f"seed must be an integer, got {type(item).__name__}")
        if not _SEED_MIN <= item <= _SEED_MAX:
            raise InvalidSeedError(f"seed {item} is outside the 64-bit seed range")
        out.append(item)
    return out


def _optional_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _validate_digest(value, field=field)


# ── Root computation ──────────────────────────────────────────────────────


def missing_components(
    *,
    environment_digest: str | None,
    seeds: Iterable[int],
    data_digest: str | None,
    code_digest: str | None,
) -> list[str]:
    """Name the required components that were not supplied, in reporting order."""
    present = {
        "environment_digest": environment_digest is not None,
        "seeds": bool(list(seeds)),
        "data_digest": data_digest is not None,
        "code_digest": code_digest is not None,
    }
    return [name for name in REQUIRED_COMPONENTS if not present[name]]


def compute_bound_root(
    *,
    environment_digest: str | None,
    seeds: Iterable[int],
    data_digest: str | None,
    code_digest: str | None,
    workflow_digest: str | None,
    determinism_class: DeterminismClass,
    capsule_root: str | None,
) -> str:
    """Content-address every present receipt component under one root.

    Canonical JSON (sorted keys, no whitespace) over the *present* components
    only, plus the determinism class, the sealed capsule root when known, and a
    domain tag. Absent components are left out rather than hashed as ``null``,
    so adding a component later always changes the root (there is no value an
    absent component "was"). Pure and deterministic: no clock, no environment.
    """
    seed_list = list(seeds)
    canonical: dict[str, Any] = {
        "domain": _ROOT_DOMAIN,
        "determinism_class": determinism_class,
    }
    optional: dict[str, Any] = {
        "environment_digest": environment_digest,
        "data_digest": data_digest,
        "code_digest": code_digest,
        "workflow_digest": workflow_digest,
        "capsule_root": capsule_root,
    }
    canonical.update({k: v for k, v in optional.items() if v is not None})
    if seed_list:
        canonical["seeds"] = seed_list
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_prefixed(blob)


# ── Objects ───────────────────────────────────────────────────────────────


class ReproducibilityReceipt(BaseModel):
    """The ``facets.science_provenance.reproducibility_receipt`` block (NF-323)."""

    model_config = ConfigDict(extra="allow")

    schema_version: str = RECEIPT_SCHEMA_VERSION
    environment_digest: str | None = None
    #: Ordered seed list. The spec's scalar ``seed`` is accepted by
    #: :func:`build_receipt` and normalised to a one-element list here.
    seeds: list[int] = Field(default_factory=list)
    data_digest: str | None = None
    code_digest: str | None = None
    workflow_digest: str | None = None
    determinism_class: DeterminismClass = "undeclared"
    #: The sealed capsule root this receipt is bound to, when known at build
    #: time (the science facet's ``bound_root``). Hashed into ``bound_root``.
    capsule_root: str | None = None
    bound_root: str
    receipt_incomplete: list[str] = Field(default_factory=list)

    @field_validator(
        "environment_digest",
        "data_digest",
        "code_digest",
        "workflow_digest",
        "capsule_root",
        mode="before",
    )
    @classmethod
    def _check_optional_digest(cls, v: object, info: ValidationInfo) -> str | None:
        return _optional_digest(v, field=str(info.field_name))

    @field_validator("bound_root", mode="before")
    @classmethod
    def _check_bound_root(cls, v: object) -> str:
        return _validate_digest(v, field="bound_root")

    @field_validator("seeds", mode="before")
    @classmethod
    def _check_seeds(cls, v: object) -> list[int]:
        return _validate_seeds(v)

    @field_validator("receipt_incomplete", mode="before")
    @classmethod
    def _check_incomplete(cls, v: object) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, (list, tuple)):
            raise ReproducibilityReceiptError("receipt_incomplete must be a list")
        unknown = sorted({str(x) for x in v} - set(REQUIRED_COMPONENTS))
        if unknown:
            raise ReproducibilityReceiptError(
                f"receipt_incomplete names unknown component(s): {unknown}"
            )
        return [str(x) for x in v]

    def recompute_root(self) -> str:
        """Recompute the root from this receipt's own components."""
        return compute_bound_root(
            environment_digest=self.environment_digest,
            seeds=self.seeds,
            data_digest=self.data_digest,
            code_digest=self.code_digest,
            workflow_digest=self.workflow_digest,
            determinism_class=self.determinism_class,
            capsule_root=self.capsule_root,
        )

    def recompute_incomplete(self) -> list[str]:
        """Recompute which required components are absent."""
        return missing_components(
            environment_digest=self.environment_digest,
            seeds=self.seeds,
            data_digest=self.data_digest,
            code_digest=self.code_digest,
        )

    @property
    def has_material(self) -> bool:
        """True when the receipt records at least one component or a declared class."""
        return (
            len(self.recompute_incomplete()) < len(REQUIRED_COMPONENTS)
            or self.workflow_digest is not None
            or self.determinism_class != "undeclared"
        )


class ReceiptVerification(BaseModel):
    """What :func:`verify_receipt` checked — and, explicitly, what it did not.

    ``re_executed`` and ``reproducible_in_fact`` are fixed: this module never
    re-runs a computation, so it can never know whether the run reproduces.
    They are emitted anyway so the absence of that claim is *visible* in every
    serialized verification, not merely implied (I-4).
    """

    model_config = ConfigDict(frozen=True)

    sealed_into_root: bool
    expected_root: str
    declared_root: str
    receipt_incomplete: list[str]
    incomplete_declared_correctly: bool
    complete: bool
    determinism_class: DeterminismClass
    re_executed: Literal[False] = False
    reproducible_in_fact: None = None

    @property
    def ok(self) -> bool:
        """True when the root re-derives and the declared incompleteness is honest."""
        return self.sealed_into_root and self.incomplete_declared_correctly


# ── Build / verify ────────────────────────────────────────────────────────


def build_receipt(
    *,
    environment_digest: str | None = None,
    seed: int | None = None,
    seeds: Iterable[int] | None = None,
    data_digest: str | None = None,
    code_digest: str | None = None,
    workflow_digest: str | None = None,
    determinism_class: DeterminismClass = "undeclared",
    capsule_root: str | None = None,
) -> ReproducibilityReceipt:
    """Build a sealed reproducibility receipt from the components supplied.

    Absent components are recorded as absent — named in ``receipt_incomplete`` —
    never fabricated. ``seed`` and ``seeds`` are the spec's two spellings; pass
    one or the other.

    Raises:
        InvalidSeedError: a seed is not an in-range integer, or both ``seed``
            and ``seeds`` were passed.
        InvalidNodeDigestError: a digest is not ``sha256:<64 hex>``.
        PayloadCaptureError: raw bytes or an over-long string reached a digest
            field (I-2).
    """
    if seed is not None and seeds is not None:
        raise InvalidSeedError("pass either seed or seeds, not both")
    seed_list = _validate_seeds(seed if seed is not None else list(seeds or []))
    env = _optional_digest(environment_digest, field="environment_digest")
    data = _optional_digest(data_digest, field="data_digest")
    code = _optional_digest(code_digest, field="code_digest")
    workflow = _optional_digest(workflow_digest, field="workflow_digest")
    root = _optional_digest(capsule_root, field="capsule_root")
    return ReproducibilityReceipt(
        environment_digest=env,
        seeds=seed_list,
        data_digest=data,
        code_digest=code,
        workflow_digest=workflow,
        determinism_class=determinism_class,
        capsule_root=root,
        bound_root=compute_bound_root(
            environment_digest=env,
            seeds=seed_list,
            data_digest=data,
            code_digest=code,
            workflow_digest=workflow,
            determinism_class=determinism_class,
            capsule_root=root,
        ),
        receipt_incomplete=missing_components(
            environment_digest=env, seeds=seed_list, data_digest=data, code_digest=code
        ),
    )


def verify_receipt(receipt: ReproducibilityReceipt) -> ReceiptVerification:
    """Offline-verify a receipt without raising (record-and-report path).

    Confirms every present digest seals into ``bound_root`` and that the declared
    ``receipt_incomplete`` list matches what is actually absent — a receipt that
    hides a missing component is as broken as one whose root does not match.
    Pure hashing; no execution, no network.
    """
    expected = receipt.recompute_root()
    incomplete = receipt.recompute_incomplete()
    return ReceiptVerification(
        sealed_into_root=expected == receipt.bound_root,
        expected_root=expected,
        declared_root=receipt.bound_root,
        receipt_incomplete=incomplete,
        incomplete_declared_correctly=sorted(incomplete) == sorted(receipt.receipt_incomplete),
        complete=not incomplete,
        determinism_class=receipt.determinism_class,
    )


# ── Capsule attach / read ─────────────────────────────────────────────────


def attach_receipt(capsule: dict[str, Any], receipt: ReproducibilityReceipt) -> dict[str, Any]:
    """Attach the receipt under ``facets.science_provenance``, additively.

    Preserves every other key already in the science facet (the NF-321 DAG, its
    ``bound_root``) and every other facet. A receipt with no material writes
    nothing — a run that recorded no reproducibility inputs stays byte-identical
    (I-1, I-3). Returns a new dict; the input is not mutated.
    """
    if not receipt.has_material:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    science = dict(facets.get(FACET_NAME) or {})
    science[RECEIPT_KEY] = receipt.model_dump(mode="json", exclude_none=True)
    facets[FACET_NAME] = science
    out["facets"] = facets
    return out


def receipt_from_capsule(capsule: dict[str, Any]) -> ReproducibilityReceipt | None:
    """Read the receipt back out of a capsule dict; None when absent (I-3).

    Raises:
        pydantic.ValidationError / ScienceProvenanceError: the block is present
            but malformed. A present-but-broken receipt is a finding, not absence.
    """
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    science = facets.get(FACET_NAME)
    if not isinstance(science, dict):
        return None
    block = science.get(RECEIPT_KEY)
    if not isinstance(block, dict):
        return None
    return ReproducibilityReceipt.model_validate(block)
