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

"""Context-window composition receipt — ADR-0143 P2 (NF-113).

An ordered, digested manifest of everything that entered the model's context,
with the token budget it consumed and a ``receipt_digest`` binding the whole
composition. This is the "what was in the model's mind, in what order, at what
token cost" evidence that context rot makes necessary.

**Order is the evidence.** The manifest is never sorted, deduplicated, or
normalised. Two runs whose context held the same items in a different order are
two different runs — context rot is precisely a position-dependent effect — so
reordering the manifest to make it prettier would destroy the fact it exists to
record. :class:`ContextReceipt` rejects a manifest whose declared ``position``
values disagree with its list order rather than repairing either.

**No content.** Every entry is a ``sha256:`` digest and a token count; the item's
text never enters the receipt (I-2, ADR-0021 §4). Raw bytes or a sequence
arriving where a digest belongs raises
:class:`~novafabric.context._refs.ContentCaptureError`.

Standalone artifact, not a capsule facet
----------------------------------------
The capsule's ``facets`` registry is closed (``additionalProperties: false``,
ADR-0196 D2) and registers no ``context_receipt`` name. Adding one is a schema
change that needs its own ADR-0196 registry slice, so P2 ships the receipt as a
**standalone artifact**: callers build, digest, and verify it, and a capsule
with no context material is byte-identical and entirely unmutated by this
module. There is deliberately no ``attach_facet`` here.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from novafabric.context._refs import (
    ReceiptOrderError,
    validate_digest,
)

SCHEMA_VERSION = "0.1.0"

#: Where a context entry came from, fixed by ADR-0143 D3 / spec §4.3. Closed on
#: purpose: an unrecognised source would be an entry whose *provenance class* no
#: later verifier can reconstruct, which is worse evidence than a rejected one.
ContextSource = Literal["system", "memory", "retrieved", "tool", "user", "history"]

#: The declared source vocabulary, as data, for callers that need to iterate it.
CONTEXT_SOURCES: tuple[str, ...] = (
    "system",
    "memory",
    "retrieved",
    "tool",
    "user",
    "history",
)


class ContextEntry(BaseModel):
    """One item that entered the context window (NF-113).

    ``position`` is redundant with the entry's index in
    :attr:`ContextReceipt.entries` and that redundancy is the point: the
    manifest is routinely re-serialised, sliced into reports, and rendered by
    tools that may not preserve list order, so each entry carries its own
    ordinal and the receipt checks the two agree.
    """

    model_config = ConfigDict(extra="allow")

    source: ContextSource
    digest: str
    #: Tokens this entry consumed. Never negative; zero is legitimate (an empty
    #: system preamble that was nonetheless present is evidence).
    token_count: int = Field(ge=0)
    position: int = Field(ge=0)

    @field_validator("digest", mode="before")
    @classmethod
    def _check_digest(cls, v: object) -> str:
        return validate_digest(v, field="entry.digest")


class TokenBudget(BaseModel):
    """What the composition cost, in tokens (NF-113).

    ``window`` is optional and defaults to ``None`` — an unknown model context
    window, not an unlimited one. Emitting ``0`` or a guessed default would make
    a run whose window nobody recorded indistinguishable from one measured
    against a real limit, and every downstream "how full was the window"
    question would silently get a fabricated answer.
    """

    model_config = ConfigDict(extra="allow")

    window: int | None = Field(default=None, ge=0)
    used: int = Field(ge=0)
    #: Per-source totals. Only sources actually present in the manifest appear:
    #: the budget is *derived from* the manifest, so a missing key means "no
    #: entry of that source is in this manifest" — a fact, not an unknown.
    by_source: dict[str, int] = Field(default_factory=dict)


class ContextReceipt(BaseModel):
    """The ordered manifest + budget + binding digest (NF-113).

    ``receipt_digest`` binds entries *and* their order *and* the budget. It is
    excluded from its own preimage, so it is a pure function of the manifest and
    a verifier recomputes it from what it can see — there is no stored value for
    a tamperer to make agree with itself.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    entries: list[ContextEntry] = Field(default_factory=list)
    token_budget: TokenBudget
    receipt_digest: str | None = None

    @model_validator(mode="after")
    def _check_order(self) -> ContextReceipt:
        """Reject a manifest whose declared order is not its real order."""
        for index, entry in enumerate(self.entries):
            if entry.position != index:
                raise ReceiptOrderError(
                    f"entry at index {index} declares position {entry.position}; "
                    "a context manifest's order is the evidence and is never "
                    "re-sorted to fit (ADR-0143 NF-113)"
                )
        return self


# ── Digest ────────────────────────────────────────────────────────────────


def _canonical_json(payload: dict[str, Any]) -> str:
    """Return the canonical serialisation used as the hash preimage.

    Sorted keys and no whitespace, so the digest is stable across Python dict
    orderings, serialisers, and language runtimes — an offline verifier written
    in something other than Python has to be able to reproduce it. This is the
    same canonicalisation the shipped audit log and the memstore ledger use.

    Note that sorting *keys* does not reorder the ``entries`` list: JSON arrays
    keep their order under ``sort_keys``, which is exactly what NF-113 needs.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def receipt_preimage(receipt: ContextReceipt) -> str:
    """Return the exact string :func:`receipt_digest` hashes.

    Exposed, not private: the preimage *is* the interoperability contract for
    any non-Python verifier, and a contract nobody can inspect is a contract
    nobody can implement.

    ``exclude_none`` is deliberate. An absent ``window`` must hash the same as a
    receipt written before the field existed; rendering it as ``null`` would
    make the digest depend on which optional fields a given NovaFabric version
    happened to know about.
    """
    payload: dict[str, Any] = json.loads(
        receipt.model_dump_json(exclude_none=True, exclude={"receipt_digest"})
    )
    return _canonical_json(payload)


def receipt_digest(receipt: ContextReceipt) -> str:
    """Return the ``sha256:`` digest binding the whole composition.

    Plain ``hashlib.sha256`` over :func:`receipt_preimage` — no Merkle tree
    (see ``novafabric.context._refs`` for why). Because the preimage carries the
    entries as an ordered JSON array, reordering two entries changes the digest,
    which is the property NF-113 needs.
    """
    from novafabric.context._refs import digest_content

    return digest_content(receipt_preimage(receipt))


def verify_receipt_digest(receipt: ContextReceipt) -> bool:
    """Return whether *receipt* carries the digest its manifest implies.

    ``False`` for a receipt with no ``receipt_digest`` at all: an unbound
    manifest has not been shown to be intact, and reporting "verified" for one
    would make an unsealed receipt indistinguishable from a checked one.
    """
    if receipt.receipt_digest is None:
        return False
    return receipt.receipt_digest == receipt_digest(receipt)


# ── Construction ──────────────────────────────────────────────────────────


def build_receipt(
    entries: Sequence[ContextEntry],
    *,
    window: int | None = None,
) -> ContextReceipt:
    """Build a sealed receipt from an ordered sequence of context entries.

    *entries* are taken **in the order given** — this function never sorts them.
    The caller is the only party that knows composition order, and inferring it
    here from token counts or sources would be a guess presented as evidence.

    ``used`` and ``by_source`` are derived from the manifest rather than
    accepted from the caller: a supplied total that disagreed with the entries
    would make the receipt self-contradicting, and there would be no way for a
    later verifier to tell which of the two was the truth.
    """
    by_source: dict[str, int] = {}
    for entry in entries:
        by_source[entry.source] = by_source.get(entry.source, 0) + entry.token_count

    receipt = ContextReceipt(
        entries=list(entries),
        token_budget=TokenBudget(
            window=window,
            used=sum(entry.token_count for entry in entries),
            by_source=by_source,
        ),
    )
    receipt.receipt_digest = receipt_digest(receipt)
    return receipt


def entry_from_content(
    content: str | bytes,
    *,
    source: ContextSource,
    position: int,
    token_count: int,
) -> ContextEntry:
    """Digest *content* and return the entry referencing it.

    A convenience for callers holding the text: the content is hashed and
    discarded here, so it never reaches a receipt field even by accident (I-2).
    ``token_count`` stays a parameter — tokenisation is model-specific and
    NovaFabric does not own a tokeniser, so counting here would either add a
    dependency or fabricate a number.
    """
    from novafabric.context._refs import digest_content

    return ContextEntry(
        source=source,
        digest=digest_content(content),
        token_count=token_count,
        position=position,
    )


__all__ = [
    "CONTEXT_SOURCES",
    "SCHEMA_VERSION",
    "ContextEntry",
    "ContextReceipt",
    "ContextSource",
    "TokenBudget",
    "build_receipt",
    "entry_from_content",
    "receipt_digest",
    "receipt_preimage",
    "verify_receipt_digest",
]
