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

"""Shared reference validation for the context-provenance layer (ADR-0143 P2).

Both the NF-113 receipt and the NF-112 grounding map are built entirely from
*references* — ``sha256:`` digests and short identifiers. Neither ever holds a
context item's text or a retrieved chunk's body (I-2, ADR-0021 §4).

The guards live here rather than in either module because a reference that
passes in one and fails in the other would be the worst outcome: the receipt
would bind a digest the grounding map had rejected, and the two halves of the
same evidence would disagree about what counts as a reference.

Hash construction — plain SHA-256, no tree
------------------------------------------
Digests here are plain ``hashlib.sha256`` over the item's bytes. The repository
carries two mutually incompatible Merkle constructions —
``evidence/merkle.py`` (RFC 6962, domain-separated leaf/inner prefixes) and
``trust/novaseal/merkle.py`` (pairwise with odd-duplicate padding) — and mixing
leaves from one with the other's combiner silently yields a wrong root that
still looks like a root. P2 needs no tree and builds none: a context manifest is
*ordered*, so a single digest over the whole ordered manifest already commits to
every entry and to their order, and no inclusion proof over an unordered set is
required. This mirrors the choice ``memstore/ledger.py`` made for the same
reason.
"""

from __future__ import annotations

import hashlib
import re

#: The one digest form the rest of the capsule uses. Matched strictly
#: (lower-case hex, exact length) so a truncated or upper-cased digest fails at
#: construction rather than silently failing to match a chunk years later,
#: during an audit of a run nobody present still remembers.
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: An identifier, never a document. A span id or document id in the wild is a
#: short opaque token; anything longer is overwhelmingly likely to be context
#: text or chunk body smuggled through an id field — exactly what I-2 exists to
#: stop.
MAX_ID_LENGTH = 512


# ── Errors ────────────────────────────────────────────────────────────────


class ContextProvenanceError(Exception):
    """Base class for every error the context-provenance layer raises.

    Subclasses :class:`Exception` rather than :class:`ValueError`: a caller
    wrapping context-evidence work wants to catch *context-provenance*
    failures, and inheriting from ValueError would also swallow unrelated
    coercion errors raised from inside the same block.
    """


class InvalidReferenceError(ContextProvenanceError):
    """Raised when a digest or identifier is malformed."""


class ContentCaptureError(ContextProvenanceError):
    """Raised when raw content arrives where a digest or id belongs.

    Distinct from :class:`InvalidReferenceError` on purpose: a malformed digest
    is a caller mistake, while a prompt fragment or chunk body reaching this
    boundary is an I-2 violation, and the two want very different fixes from
    whoever reads the traceback.
    """


class ReceiptOrderError(ContextProvenanceError):
    """Raised when a context manifest's declared order is not its real order.

    Order is the evidence this layer exists to preserve, so a manifest whose
    ``position`` values disagree with its list order is rejected rather than
    repaired: silently trusting one of the two would make readers that trust
    the other disagree about what entered the model's context first.
    """


# ── Guards ────────────────────────────────────────────────────────────────


def reject_content(value: object, *, field: str) -> None:
    """Raise if *value* is raw content rather than a reference (I-2)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ContentCaptureError(
            f"{field} must be a reference (sha256 digest or id), not raw bytes; "
            "digest the content yourself with digest_content() and pass the "
            "result (ADR-0143 I-2 — context provenance never holds contents)"
        )
    if isinstance(value, (list, tuple)):
        # An embedding vector is the payload shape most likely to arrive here by
        # accident: it is "just numbers", it does not look like content, and it
        # reconstructs the chunk well enough to matter (ADR-0021 §4).
        raise ContentCaptureError(
            f"{field} must be a single reference, not a sequence; a vector or "
            "embedding never enters context provenance (ADR-0143 I-2)"
        )


def validate_digest(value: object, *, field: str) -> str:
    """Return *value* if it is a ``sha256:`` content digest.

    A context entry must be bound by *content identity*. A URI would not be a
    binding at all — it names where something lives, not what it is, and a
    retrieved chunk is precisely the kind of thing that gets replaced
    underneath its own id.
    """
    reject_content(value, field=field)
    if not isinstance(value, str):
        raise InvalidReferenceError(f"{field} must be a string digest")
    if not DIGEST_RE.match(value):
        raise InvalidReferenceError(f"{field} must be 'sha256:<64 hex>', got {value!r}")
    return value


def validate_id(value: object, *, field: str) -> str:
    """Return *value* if it is an acceptable short identifier.

    Deliberately permissive beyond digest/URI shapes: span ids, document ids
    and corpus ids in the wild are ULIDs, vector-store keys, offsets and
    vendor-specific tokens. Forcing them into a URI would push callers to
    synthesise fake URIs, which is worse evidence than the opaque id they
    actually hold.
    """
    reject_content(value, field=field)
    if not isinstance(value, str):
        raise InvalidReferenceError(f"{field} must be a string identifier")
    if not value.strip():
        raise InvalidReferenceError(
            f"{field} must be non-empty; an unnamed span or document cannot be "
            "reconciled against the run later (ADR-0143 NF-112/NF-113)"
        )
    if len(value) > MAX_ID_LENGTH:
        raise ContentCaptureError(
            f"{field} is {len(value)} chars, over the {MAX_ID_LENGTH}-char "
            "identifier limit; this looks like inlined content, not an id"
        )
    return value


def digest_content(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a context item or retrieved chunk.

    Plain ``hashlib.sha256`` over the item's bytes — no Merkle leaf prefix, no
    tree, no domain separation (see the module docstring for why). Emitted in
    the same ``sha256:<hex>`` form the rest of the capsule uses, so a verifier
    does not have to know which subsystem wrote it.

    The content is hashed and discarded; nothing here retains it (I-2).
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"
