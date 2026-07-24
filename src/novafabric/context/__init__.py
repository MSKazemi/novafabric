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

"""Context provenance — what entered the model's context (ADR-0143, experimental).

Record-only. This layer records an ordered manifest of what entered a context
window (NF-113) and the span→chunk support map a producer claimed (NF-112). It
never scores groundedness, never asserts that a span is grounded, and never
holds context or chunk contents — only ``sha256:`` digests and short ids.

P2 ships NF-113 and NF-112 as **standalone artifacts**: the capsule ``facets``
registry is closed (ADR-0196 D2) and registers neither name, so nothing here
reads or mutates a capsule. Staleness (NF-119), eviction (NF-115), corpus pin
(NF-117) and the receipt diff (NF-118) are later slices, and there is no CLI
yet — ``nova context receipt``/``grounding`` land in their own slice with the
dashboard command-registry regeneration they require.
"""

from novafabric.context._refs import (
    ContentCaptureError,
    ContextProvenanceError,
    InvalidReferenceError,
    ReceiptOrderError,
    digest_content,
)
from novafabric.context.grounding import (
    RECORDED_NOT_SCORED_NOTE,
    ChunkRef,
    GroundingMap,
    SpanGrounding,
    SupportLabel,
    build_grounding,
    chunk_ref_for_document,
    chunk_refs_from_event,
    ground_span,
    is_recorded,
    recorded_span_ids,
    support_for,
)
from novafabric.context.receipt import (
    CONTEXT_SOURCES,
    ContextEntry,
    ContextReceipt,
    ContextSource,
    TokenBudget,
    build_receipt,
    entry_from_content,
    receipt_digest,
    receipt_preimage,
    verify_receipt_digest,
)

__all__ = [
    "CONTEXT_SOURCES",
    "RECORDED_NOT_SCORED_NOTE",
    "ChunkRef",
    "ContentCaptureError",
    "ContextEntry",
    "ContextProvenanceError",
    "ContextReceipt",
    "ContextSource",
    "GroundingMap",
    "InvalidReferenceError",
    "ReceiptOrderError",
    "SpanGrounding",
    "SupportLabel",
    "TokenBudget",
    "build_grounding",
    "build_receipt",
    "chunk_ref_for_document",
    "chunk_refs_from_event",
    "digest_content",
    "entry_from_content",
    "ground_span",
    "is_recorded",
    "receipt_digest",
    "receipt_preimage",
    "recorded_span_ids",
    "support_for",
    "verify_receipt_digest",
]
