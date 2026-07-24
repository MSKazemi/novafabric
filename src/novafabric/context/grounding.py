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

"""Retrieval→claim grounding map — ADR-0143 P2 (NF-112).

Maps an output span to the retrieved chunk(s) claimed to support it, built from
the shipped ``VectorRetrievalEvent``.

**Recorded, not scored.** NovaFabric records the support *map* a producer
claimed. It never computes an entailment or faithfulness score, never asserts
that a span *is* grounded, and never adjudicates whether the cited chunk
actually supports the text. That is an external evaluator's job (NF-099); this
module exposes no entry point for it, and
``tests/context/test_context_receipt_grounding.py`` fails if one ever appears.

**Absent is not false.** A span with no entry in the map is **not recorded** —
it is not "ungrounded". That distinction is the entire difference between
missing evidence and evidence of a problem, and conflating the two would let a
run that simply never emitted a grounding map read as a run whose every claim
was unsupported. :func:`support_for` returns ``None`` for an unrecorded span,
and :func:`is_recorded` exists so callers never have to infer absence from a
falsy value.

Note the related-but-different ``"uncited"`` label: that is a *recorded* claim
that the producer cited nothing for a span. A recorded "cited nothing" and an
absent entry are different facts and stay distinguishable here.

**No content.** Chunks are referenced by ``document_id`` and ``content_hash``;
the chunk body never enters the map (I-2, ADR-0021 §4). ``RetrievedDocument``'s
opt-in ``content`` field is deliberately never read by this module.

Standalone artifact, not a capsule facet
----------------------------------------
The capsule's ``facets`` registry is closed (``additionalProperties: false``,
ADR-0196 D2) and registers no ``grounding`` name, so P2 ships the map as a
standalone artifact and mutates no capsule. See ``receipt.py`` for the full
reasoning.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from novafabric.capture.events import RetrievedDocument, VectorRetrievalEvent
from novafabric.context._refs import validate_digest, validate_id

SCHEMA_VERSION = "0.1.0"

#: The honesty line every consumer of this map must carry forward. Stored on the
#: model so it survives serialisation into a report whose author never read this
#: docstring.
RECORDED_NOT_SCORED_NOTE = (
    "support labels are recorded as claimed, not scored here; NovaFabric does "
    "not assert that a span is grounded (ADR-0143 D2 / NF-099 external evaluator)"
)

#: The support vocabulary, fixed by ADR-0143 D2. ``uncited`` means the producer
#: recorded that it cited nothing; it is NOT the same as a span absent from the
#: map, which means nothing was recorded at all.
SupportLabel = Literal["supported", "partial", "unsupported", "uncited"]


class ChunkRef(BaseModel):
    """A reference to one retrieved chunk (NF-112).

    ``content_hash`` is optional and defaults to ``None`` — the ADR-0153 pin may
    be absent on a document captured before that slice, and ``None`` means
    "unknown", never "unmodified" (matching ``RetrievedDocument``'s own
    documented semantics).

    The store's similarity ``score`` from ``RetrievedDocument`` is deliberately
    **not** carried here. ADR-0143 does not settle the question, and a retrieval
    similarity score sitting in a field called ``supported_by`` reads as a
    support strength to every downstream consumer — which is exactly the
    "illusion of groundedness" D2 exists to avoid. ``rank`` preserves the
    retrieval ordering without implying a degree of support.
    """

    model_config = ConfigDict(extra="allow")

    document_id: str
    content_hash: str | None = None
    #: 1-based position in the retrieval result, preserving the store's order.
    rank: int | None = Field(default=None, ge=1)

    @field_validator("document_id", mode="before")
    @classmethod
    def _check_document_id(cls, v: object) -> str:
        return validate_id(v, field="chunk.document_id")

    @field_validator("content_hash", mode="before")
    @classmethod
    def _check_content_hash(cls, v: object) -> str | None:
        if v is None:
            return None
        return validate_digest(v, field="chunk.content_hash")


class SpanGrounding(BaseModel):
    """One output span and the chunks claimed to support it (NF-112)."""

    model_config = ConfigDict(extra="allow")

    span_id: str
    supported_by: list[ChunkRef] = Field(default_factory=list)
    support: SupportLabel

    @field_validator("span_id", mode="before")
    @classmethod
    def _check_span_id(cls, v: object) -> str:
        return validate_id(v, field="span.span_id")


class GroundingMap(BaseModel):
    """The span→chunk support map for one run (NF-112).

    Spans are kept in the order given. Unlike the NF-113 receipt, order here
    carries no claim about output order — but re-sorting would still discard the
    producer's sequencing for no benefit, so the list is left alone.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: str = SCHEMA_VERSION
    spans: list[SpanGrounding] = Field(default_factory=list)
    #: Optional pointer to the NF-117 corpus pin (a later slice); ``None`` here.
    corpus_pin_ref: str | None = None
    note: str = RECORDED_NOT_SCORED_NOTE

    @field_validator("corpus_pin_ref", mode="before")
    @classmethod
    def _check_corpus_pin_ref(cls, v: object) -> str | None:
        if v is None:
            return None
        return validate_digest(v, field="grounding.corpus_pin_ref")


# ── Construction from shipped events ──────────────────────────────────────


def chunk_ref_for_document(document: RetrievedDocument, *, rank: int) -> ChunkRef:
    """Return the reference for one retrieved document.

    Reads ``document_id`` and the ADR-0153 ``content_hash`` pin only. The opt-in
    ``content`` field is never read: a grounding map that carried chunk bodies
    would defeat I-2 and could itself smuggle poisoned text into the record.
    """
    return ChunkRef(
        document_id=document.document_id,
        content_hash=document.content_hash,
        rank=rank,
    )


def chunk_refs_from_event(event: VectorRetrievalEvent) -> list[ChunkRef]:
    """Return references for every document a retrieval event returned.

    ``rank`` is derived from the event's list order (1-based) rather than from
    ``score``: the store returned them in that order, and re-deriving rank from
    scores would reorder ties arbitrarily and disagree with what the model
    actually saw.

    Non-``query`` operations (``upsert``, ``delete``) return an empty list —
    they retrieve nothing, so there is no chunk any output span could cite.
    """
    if event.operation != "query":
        return []
    return [
        chunk_ref_for_document(document, rank=index)
        for index, document in enumerate(event.documents, start=1)
    ]


def build_grounding(
    spans: Sequence[SpanGrounding],
    *,
    corpus_pin_ref: str | None = None,
) -> GroundingMap:
    """Build a grounding map from already-claimed span support.

    The *claims* come from the caller. This function does not inspect span text,
    compare it against chunks, or derive a support label — deriving one would be
    scoring, which NovaFabric does not do (D2).
    """
    return GroundingMap(spans=list(spans), corpus_pin_ref=corpus_pin_ref)


def ground_span(
    span_id: str,
    *,
    support: SupportLabel,
    chunks: Iterable[ChunkRef] = (),
) -> SpanGrounding:
    """Record one span's claimed support.

    ``support`` is required and has no default. A default would mean this layer
    picking a label for a claim the producer never made — the one thing D2
    forbids — and ``"uncited"`` in particular is a positive assertion that
    nothing was cited, not a safe fallback.
    """
    return SpanGrounding(span_id=span_id, supported_by=list(chunks), support=support)


# ── Lookup ────────────────────────────────────────────────────────────────


def support_for(grounding: GroundingMap, span_id: str) -> SpanGrounding | None:
    """Return the recorded grounding for *span_id*, or ``None`` if unrecorded.

    ``None`` means **not recorded**, never "ungrounded" or "unsupported". A
    caller that renders ``None`` as a negative finding is reporting evidence of
    a problem where it only has missing evidence.
    """
    for span in grounding.spans:
        if span.span_id == span_id:
            return span
    return None


def is_recorded(grounding: GroundingMap, span_id: str) -> bool:
    """Return whether *span_id* has any recorded grounding at all.

    Exists so callers never have to infer absence from a falsy return: a span
    recorded as ``"uncited"`` with no chunks is *recorded*, and this returns
    ``True`` for it while :func:`support_for` returns a span whose
    ``supported_by`` is empty.
    """
    return support_for(grounding, span_id) is not None


def recorded_span_ids(grounding: GroundingMap) -> list[str]:
    """Return the span ids that carry a recorded claim, in map order."""
    return [span.span_id for span in grounding.spans]


__all__ = [
    "RECORDED_NOT_SCORED_NOTE",
    "SCHEMA_VERSION",
    "ChunkRef",
    "GroundingMap",
    "SpanGrounding",
    "SupportLabel",
    "build_grounding",
    "chunk_ref_for_document",
    "chunk_refs_from_event",
    "ground_span",
    "is_recorded",
    "recorded_span_ids",
    "support_for",
]
