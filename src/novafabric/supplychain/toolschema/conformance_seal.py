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

"""Sealed output-conformance evidence (ADR-0148 D2 / NF-168).

Takes the ADR-0128 ``schema_validation`` verdicts a capsule already records and makes them
**tamper-evident** rather than merely recorded, by hashing them into the in-toto attestation.

**The two halves live apart, and that is the whole point.** The verdicts go in the capsule facet;
the **digest alone** goes in the signed attestation predicate. Verification recomputes the digest
from the facet and compares it against the attestation's, so altering a recorded verdict changes
the recomputation and the comparison fails. A seal whose verification recomputes from the same
object that carries the digest re-hashes its own input and **can never fail** — that is the
vacuity this module is arranged to avoid, and a test mutates a verdict to prove it does.

**Nothing is re-validated here.** The ADR-0128 verdict is reused verbatim (D2's reuse rule): this
module hashes and counts, it does not interpret schemas.

**⚠ "Not checked" is not "conforming".** A verdict is absent when a tool call declared no
``*_schema_ref``. Counting those as conforming would let a capsule where nothing declared a schema
report perfect conformance, so ``unchecked`` is its own count.

**On reproducibility:** an ADR-0128 verdict carries ``checked_at``, so re-running validation
produces a different verdict and therefore a different digest. That is correct for a tamper seal —
it seals *this record*, not the act of validating — and a re-derived digest differing from the
sealed one is not a seal failure.

It **records; it does not gate**: violations are counted, not turned into a verdict about the run.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1.0"
FACET_NAME = "output_conformance"

#: Predicate key under which the digest is sealed into the in-toto statement. Versioned, so a
#: change to the digest construction cannot be mistaken for the same claim.
PREDICATE_KEY = "novafabric.dev/tool-schema-conformance/v1"


class ConformanceSealError(ValueError):
    """Raised when conformance evidence cannot be sealed honestly."""


class ConformanceSeal(BaseModel):
    """Recorded ADR-0128 verdicts plus the digest that makes them tamper-evident.

    Intentionally carries no pass/fail field: ``violating > 0`` is a count, and whether that
    blocks anything is not this module's decision.
    """

    model_config = ConfigDict(frozen=True)

    sealed_digest: str
    #: Tool calls considered — conforming + violating + unchecked.
    calls: int = 0
    conforming: int = 0
    violating: int = 0
    #: Calls that declared no schema, so nothing was checked. **Not** conforming.
    unchecked: int = 0
    #: The ADR-0128 verdicts, verbatim and in recorded order — what the digest is over.
    verdicts: list[dict[str, Any] | None] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION


def digest_of(verdicts: Sequence[Mapping[str, Any] | None]) -> str:
    """Digest a verdict list by canonical content.

    Order is significant and preserved: the verdicts describe a sequence of calls, and reordering
    them describes a different run.
    """
    encoded = json.dumps(
        [dict(v) if v is not None else None for v in verdicts],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_violation(verdict: Mapping[str, Any]) -> bool:
    return verdict.get("arguments_valid") is False or verdict.get("result_valid") is False


def seal_conformance(verdicts: Sequence[Mapping[str, Any] | None]) -> ConformanceSeal:
    """Seal the recorded ADR-0128 verdicts for one capsule's tool calls.

    *verdicts* is the per-call ``schema_validation`` block **as recorded**, with ``None`` for a
    call that declared no schema. Nothing is re-validated.

    Raises:
        ConformanceSealError: if no calls were supplied. A digest over an empty list is a
            constant, so every schema-free capsule would seal to the same value and compare
            equal — a seal that proves nothing while looking like one.
    """
    if not verdicts:
        raise ConformanceSealError(
            "no tool calls to seal: a digest over an empty list is a constant, so every capsule "
            "without calls would seal identically and the comparison would prove nothing"
        )
    present = [v for v in verdicts if v is not None]
    violating = sum(1 for v in present if _is_violation(v))
    return ConformanceSeal(
        sealed_digest=digest_of(verdicts),
        calls=len(verdicts),
        conforming=len(present) - violating,
        violating=violating,
        unchecked=len(verdicts) - len(present),
        verdicts=[dict(v) if v is not None else None for v in verdicts],
    )


def into_predicate(seal: ConformanceSeal) -> dict[str, Any]:
    """Return the ``extra_predicate`` fragment for the in-toto statement.

    **Carries the digest and the counts, never the verdicts.** The verdicts live in the capsule
    facet; keeping them out of the predicate is what leaves verification with two independent
    sources to compare. Pass to ``envelopes.intoto.capsule_statement(extra_predicate=...)`` —
    reusing the capsule statement rather than introducing a third top-level format (ADR-0034).
    """
    return {
        PREDICATE_KEY: {
            "sealed_digest": seal.sealed_digest,
            "calls": seal.calls,
            "conforming": seal.conforming,
            "violating": seal.violating,
            "unchecked": seal.unchecked,
            "schema_version": seal.schema_version,
        }
    }


def verify_seal(
    verdicts: Sequence[Mapping[str, Any] | None], sealed_digest: str
) -> bool:
    """Recompute the digest from *verdicts* and compare it with the sealed one.

    *verdicts* must come from the capsule facet and *sealed_digest* from the attestation
    predicate — two independent sources. Recomputing from the object that carries the digest
    would re-hash its own input and always agree.
    """
    return digest_of(verdicts) == sealed_digest


# ── Capsule facet ─────────────────────────────────────────────────────────


def attach_facet(capsule: dict[str, Any], seal: ConformanceSeal | None) -> dict[str, Any]:
    """Attach *seal* to a capsule dict additively; returns a new dict."""
    if seal is None:
        return capsule
    out = dict(capsule)
    facets = dict(out.get("facets") or {})
    facets[FACET_NAME] = seal.model_dump(exclude_none=True)
    out["facets"] = facets
    return out


def facet_from_capsule(capsule: dict[str, Any]) -> ConformanceSeal | None:
    """Read a conformance seal back out of a capsule dict, or None if absent."""
    facets = capsule.get("facets")
    if not isinstance(facets, dict):
        return None
    block = facets.get(FACET_NAME)
    if not isinstance(block, dict):
        return None
    try:
        return ConformanceSeal.model_validate(block)
    except ValueError as exc:
        raise ConformanceSealError(
            f"capsule holds an invalid {FACET_NAME} facet: {exc}"
        ) from exc


__all__ = [
    "FACET_NAME",
    "PREDICATE_KEY",
    "SCHEMA_VERSION",
    "ConformanceSeal",
    "ConformanceSealError",
    "attach_facet",
    "digest_of",
    "facet_from_capsule",
    "into_predicate",
    "seal_conformance",
    "verify_seal",
]
