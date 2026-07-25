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
"""Row- and transformation-level lineage facets (ADR-0109 NF-061/062, verifiable half).

Two additive facets that ride the existing ``LineageEdge.facets`` dict (I-1, no
``LineageStore`` signature change):

* **`transform` (NF-062)** — a content-addressed reference (SHA-256) to the
  operation that produced an edge, plus a coarse ``op_kind`` category. The sealed
  capsule keeps the operation itself; the facet stores only its digest.
* **`rows` (NF-061)** — which row(s) influenced an output, by **hash of the row
  key/primary-key**, bounded and never the raw key or any cell content.

Load-bearing invariants (ADR-0109):

* **I-2 — names/keys/hashes, never values.** Neither facet ever stores a raw row
  key, cell value, literal, or the operation's contents — only hashes/categories.
* **I-3 — fail-open.** Every builder returns a facet or ``None`` and never raises,
  so facet extraction can never break a lineage write.
* **I-4 — round-trip.** The facets are plain dicts under the existing ``facets``
  field and survive serialization unchanged.

This is the *verifiable half* — the facet format and its builders. Auto-inferring
these facets from a live run (the capture half) is a documented later wave.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, Field

TRANSFORM_FACET_KEY = "transform"
ROW_FACET_KEY = "rows"

#: Max row-key hashes retained in one facet (bounded evidence, ADR-0090 column-cap mirror).
_MAX_ROW_KEYS = 256


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class TransformFacet(BaseModel):
    """A content-addressed reference to the operation behind a lineage edge (NF-062)."""

    op_kind: str = Field(..., description="Coarse operation category, e.g. 'sql'/'pandas'/'python'")
    op_digest: str = Field(..., description="'sha256:...' content-address of the operation")
    confidence: Literal["exact", "heuristic"] = "exact"


class RowFacet(BaseModel):
    """Row-level influence, by hashed row key — never a raw key or cell content (NF-061)."""

    row_key_hashes: list[str] = Field(default_factory=list)
    truncated: bool = False
    confidence: Literal["exact", "heuristic"] = "exact"


def build_transform_facet(
    operation: str,
    *,
    op_kind: str = "unknown",
    confidence: Literal["exact", "heuristic"] = "exact",
) -> TransformFacet | None:
    """Build a :class:`TransformFacet` from an operation representation.

    *operation* is content-addressed and discarded — its bytes never enter the
    facet (I-2). Returns ``None`` for anything that is not a non-empty string
    (I-3: fail-open, never raises).
    """
    try:
        if not isinstance(operation, str) or not operation:
            return None
        kind = op_kind if isinstance(op_kind, str) and op_kind else "unknown"
        return TransformFacet(
            op_kind=kind,
            op_digest=_digest(operation.encode("utf-8")),
            confidence=confidence,
        )
    except Exception:  # pragma: no cover - defensive I-3 guard
        return None


def build_row_facet(
    row_keys: Sequence[str],
    *,
    confidence: Literal["exact", "heuristic"] = "exact",
) -> RowFacet | None:
    """Build a :class:`RowFacet` from row keys, storing only their hashes.

    Each key is hashed (I-2: the raw key never enters the facet). The set is capped
    at :data:`_MAX_ROW_KEYS`; overflow truncates and downgrades confidence to
    ``heuristic``. Returns ``None`` for an empty/invalid input (I-3: fail-open).
    """
    try:
        if not isinstance(row_keys, (list, tuple)) or not row_keys:
            return None
        keys = [str(k) for k in row_keys]
        truncated = len(keys) > _MAX_ROW_KEYS
        kept = keys[:_MAX_ROW_KEYS]
        hashes = [_digest(k.encode("utf-8")) for k in kept]
        return RowFacet(
            row_key_hashes=hashes,
            truncated=truncated,
            confidence="heuristic" if truncated else confidence,
        )
    except Exception:  # pragma: no cover - defensive I-3 guard
        return None


__all__ = [
    "ROW_FACET_KEY",
    "TRANSFORM_FACET_KEY",
    "RowFacet",
    "TransformFacet",
    "build_row_facet",
    "build_transform_facet",
]
